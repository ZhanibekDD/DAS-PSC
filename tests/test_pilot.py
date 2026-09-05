"""Deployment policy tests use synthetic host/container metadata only."""
import importlib.util
import json
import os
from pathlib import Path
from copy import deepcopy

import pytest

spec = importlib.util.spec_from_file_location("psc_pilot", Path(__file__).parents[1]/"deploy/pilot.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def example():
    cfg = {"image_id":"sha256:"+"a"*64,"port":18090}
    item = {"Image":cfg["image_id"],"Config":{"User":"10001:10001"},
        "HostConfig":{"Memory":p.GIB,"MemorySwap":p.GIB,"NanoCpus":1_000_000_000,
            "PidsLimit":128,"ReadonlyRootfs":True,"Privileged":False,"Runtime":"runc",
            "Devices":[],"DeviceRequests":[],"CapDrop":["ALL"],
            "SecurityOpt":["no-new-privileges:true"],
            "PortBindings":{"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"18090"}]},
            "LogConfig":{"Config":{"max-size":"10m","max-file":"3"}}},
        "Mounts":[{"Type":"volume","Name":p.VOLUME,"Destination":"/data"}],
        "NetworkSettings":{"Networks":{p.NETWORK:{}}}}
    return item,cfg,{"Internal":True}


def test_runtime_policy_accepts_expected_config():
    p.validate_runtime(*example())


@pytest.mark.parametrize("key,value", [("Memory",0),("MemorySwap",-1),("NanoCpus",0),
    ("PidsLimit",-1),("ReadonlyRootfs",False),("Privileged",True),("Runtime","nvidia"),
    ("Devices",[{}]),("DeviceRequests",[{}]),("CapDrop",[]),("SecurityOpt",[]),
    ("PortBindings",{"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"18090"}]})])
def test_runtime_policy_rejects_unsafe_options(key,value):
    item,cfg,network=example()
    item["HostConfig"][key]=value
    with pytest.raises(p.Blocked):
        p.validate_runtime(item,cfg,network)


def test_runtime_policy_no_shared_mounts_or_network():
    item,cfg,network=example()
    item["Mounts"].append({"Type":"bind","Destination":"/nas"})
    with pytest.raises(p.Blocked): p.validate_runtime(item,cfg,network)
    item,cfg,network=example()
    item["NetworkSettings"]["Networks"]["dnepr_default"]={}
    with pytest.raises(p.Blocked): p.validate_runtime(item,cfg,network)
    item,cfg,network=example()
    with pytest.raises(p.Blocked): p.validate_runtime(item,cfg,{"Internal":False})


def test_changed_other_containers_detected_but_pilot_ignored():
    a={"id":"old","name":"/existing-service","project":"other","restarts":0}
    assert p.changes([a],[a,{"id":"new","project":p.PROJECT}])==[]
    assert p.changes([a],[{**a,"restarts":1}])==["/existing-service"]
    assert p.changes([a],[])==["/existing-service"]


@pytest.mark.parametrize('endpoint',['ssh://someone@host','tcp://127.0.0.1:2375','unix:///run/user/1000/docker.sock'])
def test_remote_or_rootless_context_rejected(monkeypatch,endpoint):
    monkeypatch.delenv('DOCKER_HOST',raising=False); monkeypatch.delenv('DOCKER_CONTEXT',raising=False)
    monkeypatch.setattr(p,'run',lambda *a,**kw:json.dumps([{'Endpoints':{'docker':{'Host':endpoint}}}]))
    with pytest.raises(p.Blocked): p.local_engine()


def test_endpoint_override_rejected_without_docker_call(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST','ssh://other')
    monkeypatch.setattr(p,'run',lambda *a,**kw:pytest.fail('must not connect'))
    with pytest.raises(p.Blocked): p.local_engine()


def test_subprocess_error_does_not_echo_secrets(monkeypatch):
    class Result:
        returncode=1
        stderr='password=must-not-leak'
        stdout='must-not-leak'
    monkeypatch.setattr(p.subprocess,'run',lambda *a,**kw:Result())
    with pytest.raises(p.Blocked) as e: p.run('docker','compose','secret')
    assert 'must-not-leak' not in str(e.value) and 'secret' not in str(e.value)


def test_private_state_no_overwrite_and_permissions(tmp_path):
    f=tmp_path/'config.json'
    p.private_json(f,{'secret':'test'})
    assert (f.stat().st_mode & 0o777)==0o600
    with pytest.raises(FileExistsError): p.private_json(f,{'secret':'other'})
    assert json.loads(f.read_text())['secret']=='test'


def test_start_requires_exact_expected_commit(monkeypatch):
    monkeypatch.setattr(p,'readiness',lambda *a:pytest.fail('must not inspect/write'))
    for value in [None,'main','abc','0'*39,'g'*40]:
        with pytest.raises(p.Blocked): p.start(18090,value)


def test_start_refuses_existing_private_state(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'HERE',tmp_path)
    (tmp_path/'.state').mkdir()
    monkeypatch.setattr(p,'readiness',lambda *a:pytest.fail('must not deploy'))
    with pytest.raises(p.Blocked): p.start(18090,'a'*40)


def test_bad_bundle_checksum_never_loads_image(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'HERE',tmp_path)
    monkeypatch.setattr(p,'readiness',lambda *a:{'containers':[]})
    monkeypatch.setattr(p,'run',lambda *a,**kw:pytest.fail('must not invoke docker'))
    (tmp_path/'psc-image.tar.gz').write_bytes(b'corrupted')
    (tmp_path/'manifest.json').write_text(json.dumps({'commit':'a'*40,'platform':'linux/amd64',
        'image_id':'sha256:'+'b'*64,'archive_sha256':'c'*64}))
    with pytest.raises(p.Blocked): p.start(18090,'a'*40)
    assert not (tmp_path/'.state').exists()


def test_port_reserved_on_host_rejected(monkeypatch):
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); sock.listen(1)
        monkeypatch.setattr(p,'run',lambda *a,**kw:pytest.fail('port is already bound'))
        with pytest.raises(p.Blocked): p.check_port(sock.getsockname()[1])


def test_nat_only_port_mapping_rejected(monkeypatch):
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    def run(*a,**kw):
        return 'container' if a[1]=='ps' else json.dumps({'8080/tcp':[{'HostIp':'0.0.0.0','HostPort':str(port)}]})
    monkeypatch.setattr(p,'run',run)
    with pytest.raises(p.Blocked): p.check_port(port)
