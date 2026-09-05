"""Closed-pilot deployment. No daemon changes, source mounts, builds or global cleanup.

Default command is read-only preflight. start is FIRST INSTALL ONLY, explicitly
requires an expected source commit and a CI-built bundle. stop retains all data.
Credentials and a redacted container baseline stay in .state/ (owner-only).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = "das-psc-pilot"
VOLUME = PROJECT + "_psc_data"
NETWORK = PROJECT + "_psc_only"
HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Blocked(RuntimeError):
    pass


def run(*args, env=None, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked(f"Cannot execute {args[0]}: {type(exc).__name__}") from exc
    if result.returncode:
        # Do not echo command arguments or raw stderr: they may contain credentials.
        raise Blocked(f"{args[0]} {args[1] if len(args)>1 else ''} failed (exit {result.returncode})")
    return result.stdout.strip()


def local_engine():
    if not sys.platform.startswith("linux"):
        raise Blocked("This deployment kit requires Linux")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise Blocked("Docker endpoint overrides are set. Verify the local context explicitly; do not change global settings")
    context = run("docker", "context", "inspect")
    endpoint = json.loads(context)[0]["Endpoints"]["docker"]["Host"]
    if endpoint != "unix:///var/run/docker.sock":
        raise Blocked("Expected the local rootful Docker socket; remote/rootless context not supported")
    info = json.loads(run("docker", "info", "--format", "{{json .}}"))
    if info.get("OSType") != "linux":
        raise Blocked("Linux Docker Engine is required")
    version = info.get("ServerVersion", "0")
    if int(version.split(".")[0]) < 28:
        raise Blocked("Docker Engine >=28 required for loopback port isolation. Do not upgrade/restart a shared daemon automatically")
    if "runc" not in info.get("Runtimes", {}):
        raise Blocked("runc is required; do not change the default GPU runtime")
    if not all(info.get(k) for k in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")):
        raise Blocked("Kernel/Engine resource limits are not all available")
    compose = run("docker", "compose", "version", "--short").lstrip("v")
    if int(compose.split(".")[0]) < 2:
        raise Blocked("Docker Compose v2 is required")
    return info, compose


def containers():
    ids = run("docker", "ps", "-aq", "--no-trunc").split()
    if not ids:
        return []
    # Deliberately do not request environment, labels other than project, or mounts.
    template = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
                '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                '"status":{{json .State.Status}},"started":{{json .State.StartedAt}},'
                '"restarts":{{json .RestartCount}},"health":'
                '{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}')
    text = run("docker", "inspect", "--format", template, *ids)
    return [json.loads(line) for line in text.splitlines()]


def changes(before, after):
    old = {c["id"]: c for c in before if c.get("project") != PROJECT}
    new = {c["id"]: c for c in after if c.get("project") != PROJECT}
    return sorted({(old.get(cid) or new[cid])["name"] for cid in old.keys() | new.keys()
                   if old.get(cid) != new.get(cid)})


def check_port(port):
    if not 1024 <= port <= 65535:
        raise Blocked("Use an unprivileged port between 1024 and 65535")
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise Blocked(f"Port {port} is occupied. No process was stopped") from exc
    # Docker can publish using NAT without a listening userspace socket.
    ids = run("docker", "ps", "-q").split()
    if ids:
        data = run("docker", "inspect", "--format", "{{json .HostConfig.PortBindings}}", *ids)
        for line in data.splitlines():
            for bindings in (json.loads(line) or {}).values():
                if any(b.get("HostPort") == str(port) for b in (bindings or [])):
                    raise Blocked(f"Port {port} is already published by Docker")


def readiness(port):
    info, compose = local_engine()
    current = containers()
    if any(c.get("project") == PROJECT for c in current):
        raise Blocked("Pilot project already exists. First-install command refuses to replace it")
    for kind, expected in (("volume", VOLUME), ("network", NETWORK)):
        names = run("docker", kind, "ls", "--format", "{{.Name}}").splitlines()
        if expected in names:
            raise Blocked(f"Existing {kind} {expected}: inspect manually; never delete/reuse automatically")
    check_port(port)
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(mem["MemAvailable"].split()[0]) * 1024
    cpus = os.cpu_count() or 1
    if available < 4 * GIB or cpus < 2:
        raise Blocked("Need >=4 GiB currently available RAM and >=2 host CPUs for this conservative pilot")
    if os.getloadavg()[0] > cpus * 0.8:
        raise Blocked("Host load is high: postpone, do not stop other services")
    roots = [HERE, Path(info["DockerRootDir"])]
    free = []
    for root in roots:
        try:
            disk = shutil.disk_usage(root)
        except OSError as exc:
            raise Blocked("Cannot measure Docker disk space; request a read-only administrator check") from exc
        free.append(round(disk.free / GIB, 1))
        if disk.free < 10 * GIB or disk.free / disk.total < 0.10:
            raise Blocked("Need >=10 GiB and >=10% free disk; no cleanup will be attempted")
    return {"status": "PREFLIGHT_PASS", "engine": info["ServerVersion"], "compose": compose,
            "port_candidate": port, "ram_available_gib": round(available/GIB, 1),
            "cpus": cpus, "load_1m": os.getloadavg()[0], "free_disk_gib": free,
            "containers": current,
            "note": "Snapshot only. Shared kernel/disk/network still require monitoring; not a no-impact guarantee"}


def private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def config():
    path = HERE / ".state/config.json"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise Blocked("Pilot config must be an owner-only regular file")
    return json.loads(path.read_text())


def compose(cfg, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PSC_", "COMPOSE_"))}
    env.update(PSC_IMAGE=cfg["image_id"], PSC_PASSWORD=cfg["password"], PSC_PORT=str(cfg["port"]))
    return run("docker", "compose", "--project-directory", str(HERE), "--env-file", "/dev/null",
               "-p", PROJECT, "-f", str(HERE/"compose.pilot.yml"), *args, env=env, timeout=120)


def own_id(cfg):
    ids = compose(cfg, "ps", "-aq", "psc").split()
    if len(ids) != 1:
        raise Blocked("Expected exactly one pilot container")
    data = json.loads(run("docker", "inspect", "--format", "{{json .Config.Labels}}", ids[0]))
    if data.get("com.docker.compose.project") != PROJECT or data.get("com.docker.compose.service") != "psc":
        raise Blocked("Container ownership does not match the pilot")
    return ids[0]


def validate_runtime(item, cfg, network):
    h = item["HostConfig"]
    tests = [item["Image"] == cfg["image_id"], item["Config"]["User"] == "10001:10001",
             h["Memory"] == GIB, h["MemorySwap"] == GIB, h["NanoCpus"] == 1_000_000_000,
             h["PidsLimit"] == 128, h["ReadonlyRootfs"], not h["Privileged"],
             h["Runtime"] == "runc", not h.get("Devices"), not h.get("DeviceRequests"),
             "ALL" in h["CapDrop"], "no-new-privileges:true" in h["SecurityOpt"],
             h["PortBindings"] == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(cfg["port"])}]},
             set(item["NetworkSettings"]["Networks"]) == {NETWORK}, network["Internal"],
             h["LogConfig"]["Config"].get("max-size") == "10m",
             h["LogConfig"]["Config"].get("max-file") == "3"]
    volumes = [m for m in item["Mounts"] if m["Type"] != "tmpfs"]
    tests.append(len(volumes) == 1 and volumes[0]["Type"] == "volume" and
                 volumes[0]["Name"] == VOLUME and volumes[0]["Destination"] == "/data")
    if not all(tests):
        raise Blocked("Runtime isolation/limits differ from the approved configuration")


def verify(cfg):
    cid = own_id(cfg)
    # Runtime inspection stays in memory, never printed (Config contains a password).
    item = json.loads(run("docker", "inspect", cid))[0]
    network = json.loads(run("docker", "network", "inspect", NETWORK))[0]
    validate_runtime(item, cfg, network)
    if item["State"].get("Health", {}).get("Status") != "healthy":
        raise Blocked("Pilot healthcheck is not healthy")
    url = f"http://127.0.0.1:{cfg['port']}"
    with HTTP.open(url+"/health", timeout=5) as response:
        if json.load(response).get("service") != "das-psc":
            raise Blocked("Unexpected service on the pilot port")
    try:
        HTTP.open(url+"/", timeout=5).close()
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise Blocked("Unexpected unauthenticated response") from exc
    else:
        raise Blocked("Authentication is not enforced")
    token = base64.b64encode(("psc:"+cfg["password"]).encode()).decode()
    with HTTP.open(urllib.request.Request(url+"/", headers={"Authorization":"Basic "+token}), timeout=5) as response:
        if response.status != 200:
            raise Blocked("Authenticated UI smoke failed")
    baseline = json.loads((HERE/".state/baseline.json").read_text())["containers"]
    changed = changes(baseline, containers())
    if changed:
        raise Blocked("Non-pilot container state changed: "+", ".join(changed)+". Investigate; do not restart them")
    return {"status":"PILOT_PASS", "url_on_server":url, "source_commit":cfg["commit"],
            "cpu_limit":1, "ram_limit_gib":1, "gpu":"not attached", "other_containers":"unchanged in this snapshot"}


def start(port, expected_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit or ""):
        raise Blocked("start requires --expect-commit with the reviewed full commit SHA")
    if (HERE/".state").exists():
        raise Blocked("Private pilot state exists. Refusing to overwrite; use verify/stop or inspect manually")
    baseline = readiness(port)
    manifest = json.loads((HERE/"manifest.json").read_text())
    if manifest["commit"] != expected_commit or manifest["platform"] != "linux/amd64":
        raise Blocked("Bundle source commit/platform mismatch")
    if os.uname().machine != "x86_64":
        raise Blocked("This bundle is only built for x86_64")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["image_id"]):
        raise Blocked("Invalid image ID")
    digest = hashlib.sha256()
    with (HERE/"psc-image.tar.gz").open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest["archive_sha256"]:
        raise Blocked("Image archive checksum mismatch")
    for name in ("pilot.py", "compose.pilot.yml"):
        if hashlib.sha256((HERE/name).read_bytes()).hexdigest() != manifest["files"][name]:
            raise Blocked("Deployment file checksum mismatch")
    run("docker", "load", "--input", str(HERE/"psc-image.tar.gz"), timeout=180)
    actual = run("docker", "image", "inspect", "--format", "{{.Id}}", manifest["image_id"])
    if actual != manifest["image_id"]:
        raise Blocked("Loaded image ID mismatch")
    # Recheck immediately before changing anything; a preflight is not a reservation.
    baseline = readiness(port)
    (HERE/".state").mkdir(mode=0o700)
    cfg = {"port":port,"password":secrets.token_hex(24),"image_id":actual,"commit":expected_commit}
    private_json(HERE/".state/config.json", cfg)
    private_json(HERE/".state/baseline.json", baseline)
    try:
        compose(cfg, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "psc")
    except Blocked:
        try:
            run("docker", "stop", own_id(cfg), timeout=60)
        except Blocked:
            pass
        raise Blocked("Pilot creation failed. Only its own container was eligible for stopping; data retained") from None
    last = None
    for _ in range(60):
        try:
            result = verify(cfg)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("Login: psc. Password is in .state/config.json (private, do not share it in chat).")
            return
        except (Blocked, OSError, urllib.error.URLError) as exc:
            last = exc
            time.sleep(2)
    # Stop ONLY the project we just created, retaining its database for diagnosis.
    run("docker", "stop", own_id(cfg), timeout=60)
    raise Blocked(f"Pilot stopped after failed acceptance: {last}. No volume was removed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["preflight","start","verify","stop"], default="preflight")
    parser.add_argument("--port", type=int, default=18090)
    parser.add_argument("--expect-commit")
    args = parser.parse_args()
    try:
        if args.action == "preflight":
            print(json.dumps(readiness(args.port), ensure_ascii=False, indent=2))
        elif args.action == "start":
            start(args.port, args.expect_commit)
        else:
            local_engine()
            cfg = config()
            if args.action == "verify":
                print(json.dumps(verify(cfg), ensure_ascii=False, indent=2))
            else:
                run("docker", "stop", own_id(cfg), timeout=60)
                print("Only the pilot was stopped; data retained")
    except (Blocked, OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
