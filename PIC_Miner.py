import os
import json
import time
import hashlib
import secrets
import threading
import urllib.request
import urllib.parse
import urllib.error
import multiprocessing as mp

POOL_URL = os.getenv("PICOIN_POOL_URL", "http://pi-coin.serv00.net:51431")
DATA_DIR = "data"
MINER_CONFIG = os.path.join(DATA_DIR, "miner_config.json")
MINER_NAME = os.getenv("PICOIN_MINER_NAME", "").strip()

HEARTBEAT_INTERVAL = 3.0
WORK_REFRESH_INTERVAL = 3.0
HTTP_TIMEOUT = 4.0

PI_FLEX_ALGORITHM = "PI-Flex-v2"
PI_FLEX_SCRYPT_N = 256
PI_FLEX_SCRYPT_R = 1
PI_FLEX_SCRYPT_P = 1
PI_FLEX_SALT = b"PI-Flex-v2"
PI_FLEX_MAXMEM = 4 * 1024 * 1024

CPU_COUNT = os.cpu_count() or 1
try:
    ENV_WORKERS = int(os.getenv("PICOIN_WORKERS", "0"))
except ValueError:
    ENV_WORKERS = 0
WORKER_COUNT = max(1, min(16, ENV_WORKERS if ENV_WORKERS > 0 else CPU_COUNT))
NONCE_MASK = 0xFFFFFFFFFFFFFFFF

RESET = "\033[0m"
ORANGE = "\033[38;5;208m"
GREEN = "\033[92m"
CYAN = "\033[96m"
RED = "\033[91m"
GRAY = "\033[90m"
YELLOW = "\033[93m"

if os.name == "nt":
    os.system("")


def load_config():
    if not os.path.exists(MINER_CONFIG):
        return {}
    try:
        with open(MINER_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(**updates):
    os.makedirs(DATA_DIR, exist_ok=True)
    data = load_config()
    data.update(updates)
    with open(MINER_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def load_wallet():
    return load_config().get("wallet_address")


def save_wallet(address):
    save_config(wallet_address=address)


def get_wallet():
    wallet = load_wallet()
    if wallet:
        return wallet
    print(CYAN + "Wallet setup" + RESET)
    print("\nEnter your PI-Coin wallet address:\n")
    while True:
        address = input("> ").strip()
        if address.startswith("PIC") and len(address) >= 10:
            save_wallet(address)
            print("\n" + GREEN + "Wallet saved!" + RESET + "\n")
            return address
        print("\n" + RED + "Invalid wallet address." + RESET + "\n")


def get_miner_name():
    # Environment variable intentionally overrides the saved interactive setting.
    if MINER_NAME:
        return MINER_NAME[:40]
    saved = str(load_config().get("miner_name", "")).strip()
    if saved:
        return saved[:40]
    print(CYAN + "Miner name setup" + RESET)
    print("Enter a name for this miner (e.g. Dell-OptiPlex):")
    while True:
        name = input("> ").strip()
        if 1 <= len(name) <= 40:
            save_config(miner_name=name)
            print(GREEN + "Miner name saved!" + RESET + "\n")
            return name
        print(RED + "Name must be 1-40 characters." + RESET)


def get_json(endpoint):
    request = urllib.request.Request(
        POOL_URL + endpoint,
        headers={"User-Agent": "PI-Coin-PC-Miner/PI-Flex-v7"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            return json.loads(raw) if raw else {"error": f"HTTP {e.code}"}
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception:
        return None


def send_json(endpoint, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        POOL_URL + endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PI-Coin-PC-Miner/PI-Flex-v7"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            return json.loads(raw) if raw else {"error": f"HTTP {e.code}"}
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception:
        return None


def pi_flex_hash(index, previous_hash, wallet, nonce):
    data = f"{index}|{previous_hash}|{wallet}|{nonce}".encode("utf-8")
    return hashlib.scrypt(
        password=data,
        salt=PI_FLEX_SALT,
        n=PI_FLEX_SCRYPT_N,
        r=PI_FLEX_SCRYPT_R,
        p=PI_FLEX_SCRYPT_P,
        dklen=32,
        maxmem=PI_FLEX_MAXMEM,
    ).hex()

def difficulty_target_hex(difficulty):
    target = int(16.0 ** (64.0 - float(difficulty)))
    return f"{target:064x}"


def meets_difficulty(hex_hash, difficulty):
    return hex_hash < difficulty_target_hex(difficulty)

def meets_difficulty(hex_hash, difficulty):
    d = max(0.0, float(difficulty))
    whole = int(d)
    if whole and hex_hash[:whole] != "0" * whole:
        return False
    fraction = d - whole
    if fraction <= 1e-12:
        return True
    return int(hex_hash[whole], 16) < 16 ** fraction


def pin_worker(worker_id):
    try:
        import psutil
        logical = min(CPU_COUNT, 16)
        psutil.Process().cpu_affinity([worker_id % logical])
    except Exception:
        pass


def mining_worker(worker_id, index, previous_hash, wallet, difficulty, stop_event, message_queue):
    pin_worker(worker_id)
    seed = secrets.randbits(64)
    nonce = (seed + worker_id) & NONCE_MASK
    step = WORKER_COUNT
    total = 0
    interval = 0
    last_report = time.perf_counter()
    target_hex = difficulty_target_hex(difficulty)

    while not stop_event.is_set():
        result = pi_flex_hash(index, previous_hash, wallet, nonce)
        total += 1
        interval += 1

        if result < target_hex:
            try:
                message_queue.put(("share", worker_id, nonce, result), timeout=0.2)
            except Exception:
                pass
            stop_event.set()
            return

        nonce = (nonce + step) & NONCE_MASK

        if interval >= 4096:
            now = time.perf_counter()
            elapsed = now - last_report
            if elapsed >= 0.25:
                try:
                    message_queue.put(("rate", worker_id, interval / elapsed, total), timeout=0.1)
                except Exception:
                    pass
                interval = 0
                last_report = now

    now = time.perf_counter()
    elapsed = now - last_report
    if interval and elapsed > 0:
        try:
            message_queue.put(("rate", worker_id, interval / elapsed, total), timeout=0.05)
        except Exception:
            pass


def heartbeat_loop(wallet, hashrate_data, running):
    connected = False
    while running.is_set():
        result = send_json(
            "/api/miner/heartbeat",
            {"wallet": wallet, "miner_name": MINER_NAME, "hashrate": float(hashrate_data["value"])}
        )
        if result and not connected:
            print("\n" + GREEN + "[POOL] Connected" + RESET)
            connected = True
        elif result is None and connected:
            print("\n" + RED + "[POOL] Disconnected" + RESET)
            connected = False
        running.wait(HEARTBEAT_INTERVAL)


def get_work(wallet):
    query = urllib.parse.urlencode({"wallet": wallet, "miner_name": MINER_NAME})
    return get_json("/api/work?" + query)


def submit_share(wallet, work, nonce, hash_result):
    payload = {
        "wallet": wallet,
        "miner_name": MINER_NAME,
        "index": int(work["index"]),
        "previous_hash": str(work["previous_hash"]),
        "nonce": int(nonce),
        "hash": hash_result,
        "pow_algorithm": PI_FLEX_ALGORITHM,
    }
    # Newer pool builds bind each share to the work ticket. Keep compatibility
    # with older pool builds that do not return one.
    work_ticket = work.get("work_ticket")
    if work_ticket:
        payload["work_ticket"] = str(work_ticket)
    return send_json("/api/share", payload)


def format_hashrate(rate):
    if rate >= 1_000_000_000:
        return f"{rate / 1_000_000_000:.2f} GH/s"
    if rate >= 1_000_000:
        return f"{rate / 1_000_000:.2f} MH/s"
    if rate >= 1_000:
        return f"{rate / 1_000:.2f} kH/s"
    return f"{rate:.2f} H/s"


def same_work(a, b):
    if not a or not b:
        return False
    return (
        int(a.get("index", -1)) == int(b.get("index", -2))
        and str(a.get("previous_hash", "")) == str(b.get("previous_hash", ""))
        and abs(float(a.get("share_difficulty", 0)) - float(b.get("share_difficulty", 0))) < 1e-12
        and str(a.get("work_ticket", "")) == str(b.get("work_ticket", ""))
    )


def stop_workers(processes, stop_event):
    stop_event.set()
    for p in processes:
        p.join(timeout=0.75)
    for p in processes:
        if p.is_alive():
            p.terminate()
    for p in processes:
        p.join(timeout=0.25)


def start_workers(ctx, index, previous_hash, wallet, difficulty, stop_event, message_queue):
    processes = []
    for worker_id in range(WORKER_COUNT):
        p = ctx.Process(
            target=mining_worker,
            args=(worker_id, index, previous_hash, wallet, difficulty, stop_event, message_queue),
        )
        p.daemon = True
        p.start()
        processes.append(p)
    return processes


def mine():
    global MINER_NAME
    wallet = get_wallet()
    MINER_NAME = get_miner_name()
    os.system("cls" if os.name == "nt" else "clear")

    print(ORANGE + "========================================" + RESET)
    print(ORANGE + "       PI-Coin Miner / PI-Flex v7" + RESET)
    print(ORANGE + "========================================" + RESET)
    print("Wallet:", wallet)
    print("Miner:", MINER_NAME)
    print("Pool:", POOL_URL)
    print("CPU workers:", WORKER_COUNT, "/", CPU_COUNT)
    print("PI-Flex: N=256 r=1 p=1")
    print(GREEN + "CPU mode: FULL" + RESET)
    print(GRAY + "----------------------------------------" + RESET)

    running = threading.Event()
    running.set()
    hashrate_data = {"value": 0.0}
    heartbeat_thread = threading.Thread(target=heartbeat_loop, args=(wallet, hashrate_data, running), daemon=True)
    heartbeat_thread.start()

    ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context()
    message_queue = ctx.Queue(maxsize=256)

    current_work = None
    rate_by_worker = {}
    last_rate_print = time.monotonic()
    last_work_fetch = 0.0

    try:
        while True:
            fresh = get_work(wallet)
            now = time.monotonic()
            last_work_fetch = now
            if not fresh:
                print("\n" + RED + "[POOL] Work request failed" + RESET)
                time.sleep(1.5)
                continue

            current_work = fresh
            index = int(current_work["index"])
            previous_hash = str(current_work["previous_hash"])
            share_difficulty = float(current_work.get("share_difficulty", 5.5))

            print("\n" + CYAN + "[WORK] New template" + RESET)
            print("Index:", index)
            print("Block difficulty:", f"{float(current_work.get('network_difficulty', 0)):.4f}")
            print("Share difficulty:", f"{share_difficulty:.1f}")

            stop_event = ctx.Event()
            processes = start_workers(ctx, index, previous_hash, wallet, share_difficulty, stop_event, message_queue)
            found = None
            work_changed = False
            rate_by_worker.clear()
            last_rate_print = time.monotonic()
            last_work_fetch = time.monotonic()

            try:
                while True:
                    try:
                        while True:
                            msg = message_queue.get_nowait()
                            if not msg:
                                continue
                            if msg[0] == "rate":
                                rate_by_worker[msg[1]] = float(msg[2])
                            elif msg[0] == "share":
                                found = (int(msg[2]), str(msg[3]))
                                stop_event.set()
                                break
                    except Exception:
                        pass

                    if found:
                        break

                    now = time.monotonic()
                    if now - last_rate_print >= 1.0:
                        total_rate = sum(rate_by_worker.values())
                        hashrate_data["value"] = total_rate
                        print("\r" + ORANGE + "Hashrate: " + format_hashrate(total_rate) + RESET + " | Workers: " + str(len(rate_by_worker)) + "/" + str(WORKER_COUNT) + " | Share diff: " + f"{share_difficulty:.1f}   ", end="", flush=True)
                        last_rate_print = now

                    if now - last_work_fetch >= WORK_REFRESH_INTERVAL:
                        fresh = get_work(wallet)
                        last_work_fetch = now
                        if fresh and not same_work(current_work, fresh):
                            current_work = fresh
                            work_changed = True
                            stop_event.set()
                            break

                    if not any(p.is_alive() for p in processes):
                        break

                    time.sleep(0.01)
            finally:
                stop_workers(processes, stop_event)

            if found:
                nonce, hash_result = found

                print("\n" + CYAN + "[SHARE] Pre-check..." + RESET)
                recalculated = pi_flex_hash(index, previous_hash, wallet, nonce)
                if recalculated != hash_result:
                    print(YELLOW + "[SHARE] Pre-check failed: hash mismatch - NOT sending" + RESET)
                    continue

                if not meets_difficulty(hash_result, share_difficulty):
                    print(YELLOW + f"[SHARE] Pre-check failed: below {share_difficulty:.1f} - NOT sending" + RESET)
                    continue

                check = get_work(wallet)
                if not check:
                    print(YELLOW + "[SHARE] Pre-check failed: cannot refresh work - NOT sending" + RESET)
                    continue

                check_index = int(check.get("index", -1))
                check_previous = str(check.get("previous_hash", ""))
                check_diff = float(check.get("share_difficulty", -1.0))

                if check_index != index or check_previous != previous_hash:
                    print(YELLOW + "[SHARE] Pre-check failed: blockchain changed - NOT sending" + RESET)
                    continue

                if not meets_difficulty(hash_result, check_diff):
                    print(YELLOW + f"[SHARE] Pre-check failed: fresh difficulty {check_diff:.1f} - NOT sending" + RESET)
                    continue

                # Recalculate immediately against the fresh work snapshot.
                fresh_hash = pi_flex_hash(check_index, check_previous, wallet, nonce)
                if fresh_hash != hash_result:
                    print(YELLOW + "[SHARE] Pre-check failed: fresh hash mismatch - NOT sending" + RESET)
                    continue

                print(GREEN + f"[SHARE] Pre-check OK | nonce={nonce} diff={check_diff:.1f}" + RESET)
                print(GREEN + f"[SHARE] Sending nonce={nonce} diff={check_diff:.1f}" + RESET)
                result = submit_share(wallet, check, nonce, hash_result)
                if result is None:
                    print(RED + "[POOL] Share failed" + RESET)
                    continue
                if result.get("accepted"):
                    print(GREEN + "[POOL] Share accepted" + RESET)
                else:
                    print(YELLOW + "[POOL] Share rejected: " + str(result.get("error", "Unknown reason")) + RESET)
                if result.get("block_found"):
                    print(YELLOW + "\n========================================" + RESET)
                    print(YELLOW + "             BLOCK FOUND!" + RESET)
                    print(YELLOW + "========================================" + RESET)
                    block = result.get("block") or {}
                    print("Block:", block.get("index"))
                    print("Hash:", block.get("hash"))
                    print("Reward:", block.get("reward"), "PIC")
                    print("Block time:", result.get("block_time"), "seconds")
                    print("Old difficulty:", result.get("old_difficulty"))
                    print("New difficulty:", result.get("new_difficulty"))
                continue

            if work_changed:
                continue
    finally:
        running.clear()
        heartbeat_thread.join(timeout=1.0)


if __name__ == "__main__":
    mp.freeze_support()
    try:
        mine()
    except KeyboardInterrupt:
        print("\n\n" + RED + "Miner stopped." + RESET + "\n")
