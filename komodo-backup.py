#!/usr/bin/env python3
import subprocess
import sys
import time
import logging
import os
from datetime import datetime
import requests

# --- CONFIGURATION (Safe for Public GitHub) ---
LIVE_SUBVOLUME = "/srv/dev-disk-by-uuid-ccfbe976-2415-4c9c-83c8-4288903b3725/config"
SNAPSHOT_DIR = "/srv/dev-disk-by-uuid-ccfbe976-2415-4c9c-83c8-4288903b3725/.snapshots/config"
LOG_FILE = "/var/log/komodo-backup.log"

# Path to your external configuration file containing sensitive keys
ENV_FILE_PATH = "/etc/komodo-backup.env"

# Komodo API Endpoint
KOMODO_API = "http://localhost:9120/execute"

# STACKS BLACKLIST: Stacks that will never be stopped during backup execution
IGNORED_STACKS = ["komodo", "komodo-core", "infrastructure", "nginx-proxy"]

# Wait Queue (6 retries * 5s = max 30 seconds)
MAX_RETRIES = 6         
RETRY_INTERVAL = 5      

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# Generate snapshot execution timestamp
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
snapshot_path = f"{SNAPSHOT_DIR}/backup_snap_{timestamp}"


def load_env_file(filepath):
    """Parses keys and values explicitly from an external configuration file."""
    env_vars = {}
    if not os.path.exists(filepath):
        logging.critical(f"CRITICAL: Environment configuration file missing at {filepath}!")
        sys.exit(1)
        
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    # Strip spaces and optional wrapping quotes
                    env_vars[key.strip()] = value.strip().strip('"').strip("'")
        logging.info(f"Successfully loaded external environment properties from {filepath}.")
    except Exception as e:
        logging.error(f"Error reading environment configuration file {filepath}: {e}")
        sys.exit(1)
    return env_vars


def run_command(cmd, check=True, cwd=None, preexec_fn=None, extra_env=None):
    """Executes a system process, passing down inherited and loaded environment schemas."""
    logging.info(f"Executing command: {' '.join(cmd)}")
    
    # Copy host system environment base
    current_env = os.environ.copy()
    
    # Inject loaded configuration credentials dynamically
    if extra_env:
        current_env.update(extra_env)
    
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, env=current_env, preexec_fn=preexec_fn
    )
    if check and result.returncode != 0:
        logging.error(f"ERROR executing command: {' '.join(cmd)}")
        if result.stderr:
            logging.error(f"Command stderr output: {result.stderr.strip()}")
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )
    return result.stdout


def get_running_stacks_from_docker(config_env):
    """Queries the docker daemon to extract all active compose infrastructure stacks."""
    cmd = ['docker', 'ps', '--format', '{{.Label "com.docker.compose.project"}}']
    stdout = run_command(cmd, extra_env=config_env)
    
    stacks = set()
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if line:
            stacks.add(line)
    return list(stacks)


def set_low_priority():
    """Forces the spawned child process into low scheduling bands directly during kernel fork."""
    import os
    # Minimize CPU priority allocation (19 = lowest scheduling priority)
    os.nice(19)
    
    # Direct x86_64 system call (251 = __NR_ioprio_set) forcing IO class 3 (Idle)
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.syscall(251, 1, 0, 24576)
    except Exception:
        pass


def main():
    logging.info("=== Komodo/Restic Snapshot Backup Task Initiated ===")
    
    # Explicitly load runtime properties from local secrets vault
    config_env = load_env_file(ENV_FILE_PATH)
    
    # Extract keys safely for internal API processing
    komodo_key = config_env.get("KOMODO_KEY")
    komodo_secret = config_env.get("KOMODO_SECRET")
    
    if not komodo_key or not komodo_secret:
        logging.critical("CRITICAL: KOMODO_KEY or KOMODO_SECRET missing from environment file!")
        sys.exit(1)

    stopped_stacks = []
    api_headers = {
        "X-API-Key": komodo_key,
        "X-API-Secret": komodo_secret,
        "Content-Type": "application/json"
    }

    try:
        # 1. Query production stacks directly from docker daemon engine
        logging.info("Gathering active stacks via Docker labels...")
        active_stacks = get_running_stacks_from_docker(config_env)
        logging.info(f"Discovered active container stacks: {', '.join(active_stacks)}")
        
        # 2. Sequential stack degradation based on discovery and filter rules
        for stack_name in active_stacks:
            if stack_name in IGNORED_STACKS:
                logging.info(f"Skipping protected stack execution profile (Blacklisted): {stack_name}")
                continue
                
            logging.info(f"Dispatching Stop command for stack target: {stack_name}")
            stop_res = requests.post(
                KOMODO_API,
                json={"type": "StopStack", "params": {"stack": stack_name}},
                headers=api_headers,
                timeout=30
            )
            stop_res.raise_for_status()
            stopped_stacks.append(stack_name)

        # 3. Execution confirmation wait queue
        logging.info("Verifying application processing shutdown cycles...")
        success = False
        
        for attempt in range(1, MAX_RETRIES + 1):
            current_stacks = get_running_stacks_from_docker(config_env)
            production_stacks_still_running = [s for s in current_stacks if s not in IGNORED_STACKS]
            
            if not production_stacks_still_running:
                logging.info(f"Success! All production containers halted (Confirmed on attempt {attempt}).")
                success = True
                break
            else:
                logging.warning(
                    f"Attempt {attempt}/{MAX_RETRIES}: Operational stacks still processing shutdown: "
                    f"{', '.join(production_stacks_still_running)}. Re-evaluating in {RETRY_INTERVAL}s..."
                )
                time.sleep(RETRY_INTERVAL)
        
        if not success:
            raise RuntimeError("TIMEOUT: Production infrastructure stacks failed to halt cleanly. Terminating sequence!")

        # 4. Generate Btrfs Read-Only atomic filesystem snapshot freeze
        logging.info("Freezing subvolume state using atomic Btrfs read-only snapshot...")
        run_command(["mkdir", "-p", SNAPSHOT_DIR], extra_env=config_env)
        run_command(["btrfs", "subvolume", "snapshot", "-r", LIVE_SUBVOLUME, snapshot_path], extra_env=config_env)

    except requests.exceptions.HTTPError as http_err:
        logging.error(f"API EXCEPTION: Komodo endpoint rejected transaction: {http_err}")
        raise
    except Exception as e:
        logging.error(f"CRITICAL PROCESS INTERRUPTION during lifecycle loop: {e}")
        raise
    finally:
        # 5. Recovery Rollback Block: Reinitialize only instances intentionally degraded
        if stopped_stacks:
            logging.info("=== Restoring container infrastructure runtime profiles via Komodo API ===")
            for stack_name in stopped_stacks:
                try:
                    logging.info(f"Dispatching Start command for stack target: {stack_name}")
                    res = requests.post(
                        KOMODO_API, 
                        json={"type": "StartStack", "params": {"stack": stack_name}},
                        headers=api_headers,
                        timeout=30
                    )
                    res.raise_for_status()
                    logging.info(f"Stack infrastructure {stack_name} returned to running state.")
                except Exception as start_error:
                    logging.critical(f"Recovery failed for targeted stack module {stack_name}: {start_error}")
        else:
            logging.info("No container infrastructure modifications recorded. Recovery cycle bypassed.")

    # 6. Restic Offsite Payload Sync (Safely executed outside core stack downtime window)
    logging.info("=== Maintenance downtime concluded. Initiating background Restic transfer ===")
    try:
        # Runs natively passing parsed configuration variables while bound to low-priority hooks
        run_command(
            ["/usr/bin/restic", "backup", "--tag", "config-backup", "."], 
            cwd=snapshot_path,
            preexec_fn=set_low_priority,
            extra_env=config_env
        )
        logging.info("Restic synchronization transaction to cloud target completed successfully.")
    except Exception as restic_error:
        logging.error(f"Restic synchronization lifecycle failed: {restic_error}")
        raise
    finally:
        # Always purge transient local storage structures to maintain flat volume layers
        logging.info("Purging transient local Btrfs backup snapshot allocation...")
        run_command(["btrfs", "subvolume", "delete", snapshot_path], check=False, extra_env=config_env)

    logging.info("=== Backup Operations Completed Successfully ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
