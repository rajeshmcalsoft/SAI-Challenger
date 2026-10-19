# SAI-Challenger PoC runbook

Owner: Rajesh. Targets: saivs (sairedis), saivs-thrift (saithrift), saivpp (VPP). One shared VM, no switch hardware.

## 1. What this is

Pytest suite on top of SAI-Challenger. Level 1 (control plane) creates, reads, updates and removes SAI objects and
checks the answers; it runs on every target. Level 2 (data plane) sends packets with PTF and checks where they come
out; it runs only on saivpp with --traffic.

    test (pytest) -> SAI-Challenger library -> Redis -> syncd -> libSAI (saivs | saivpp) -> VPP -> veth ports <-> PTF

## 2. Prerequisites

| Need | Check |
|---|---|
| Ubuntu 22.04 or 24.04, 8 vCPUs | lsb_release -a; nproc |
| 16 GB RAM minimum, 32 GB safer | free -g; docker stats --no-stream |
| 60 GB free disk | df -h /var/lib/docker /home |
| Docker with privileged containers | docker run --rm --privileged hello-world |
| GitHub and Docker Hub access | git ls-remote https://github.com/opencomputeproject/SAI-Challenger.git; docker pull hello-world |

This PoC VM: Ubuntu 24.04.5 LTS, 4 vCPUs, 7 GB RAM, 64 GB free disk, Docker 29.1.3. That is below the
recommended 8 vCPUs and 16 GB, so the plan runs the saithrift container instead of (not next to) the saivs container.

## 3. Shared VM setup

One account per user, all in the docker group; shared copy in /opt/sai-poc; lock files in /opt/sai-poc/locks
(saivs.lock, saivpp.lock, saivs-thrift.lock, mode 666). Own clone per user in ~/SAI-Challenger; create
tests/poc/reports before the containers start, otherwise Docker creates the mount folder as root.
Facts of this VM:

- Users are in the docker group. /opt/sai-poc is owned by root:docker with the setgid bit (drwxrwsr-x).
- Lock files: /opt/sai-poc/locks/saivs.lock, saivpp.lock, saivs-thrift.lock (mode 666).
- Shared clone: /opt/sai-poc/SAI-Challenger. A user who is not its owner gets "detected dubious ownership" from git
  there; this does not affect run_poc.sh. To use git there anyway:
  git config --global --add safe.directory /opt/sai-poc/SAI-Challenger

One-time setup (plan commands, mentor with sudo):

    for u in <user1> <user2> <user3>; do sudo adduser --gecos "" $u; sudo usermod -aG docker $u; done
    sudo mkdir -p /opt/sai-poc/locks
    sudo chgrp -R docker /opt/sai-poc && sudo chmod -R 2775 /opt/sai-poc
    sudo touch /opt/sai-poc/locks/saivs.lock /opt/sai-poc/locks/saivpp.lock /opt/sai-poc/locks/saivs-thrift.lock
    sudo chmod 666 /opt/sai-poc/locks/*.lock
    cd /opt/sai-poc && git clone https://github.com/<team-fork>/SAI-Challenger.git
    cd SAI-Challenger && git submodule update --init

Own copy (each user):

    git clone https://github.com/<team-fork>/SAI-Challenger.git ~/SAI-Challenger
    cd ~/SAI-Challenger && git submodule update --init
    mkdir -p tests/poc/reports      # must exist before the containers start

## 4. Build and start

From /opt/sai-poc/SAI-Challenger, with M holding the three -v mounts (one tests/poc folder per user):

    ./build.sh -i standalone -o deb13
    ./run.sh -i standalone -o deb13 $M
    ./exec.sh --no-tty pip3 install /sai-challenger/common /sai-challenger
    ./run.sh -a vpp -t saivpp -p -o trixie $M
    ./exec.sh -a vpp -t saivpp --no-tty pip3 install /sai-challenger/common /sai-challenger
    ./run.sh -i standalone -s thrift -o deb13 $M          # saithrift, only one of saivs/saivs-thrift at a time if memory is tight

Options -p and -o must come before $M (run.sh treats everything after the first unknown option as Docker options).
Check mounts: ./exec.sh --no-tty ls /sai-challenger/tests | grep poc_
Stop: ./run.sh <same options> -c stop (post in the team chat first).
If the saivs build fails, run.sh falls back to the prebuilt image. If Docker Hub is blocked, saivpp can be built with
./build.sh -a vpp -t saivpp -o trixie (several hours).

## 5. Target profiles

tests/poc/targets/<name>.json holds the testbed, exec options, lock name, usable ports, traffic filters and
expected_gaps.

Fields (all read by tests/poc/conftest.py or scripts/poc/run_poc.sh):

| Field | Read by | Meaning |
|---|---|---|
| name | humans | label of the profile (the file name is what selects it) |
| testbed | run_poc.sh | passed to pytest as --testbed=<value> |
| exec_args | run_poc.sh | options for exec.sh that pick the container, for example -a vpp -t saivpp |
| lock | run_poc.sh | lock file /opt/sai-poc/locks/<lock>.lock taken with flock for the whole run |
| ports | conftest.py (poc_ports) | usable port indices; poc_ports(n) returns the first n and skips the test if fewer |
| traffic_filters | conftest.py | list of frame filters; "mld" turns the MLD filter on when --mld-filter is auto |
| expected_gaps | conftest.py | test name to reason; each named test is marked as strict xfail |

Profile in use for saivpp:

    {
      "name": "saivpp",
      "testbed": "saivpp_standalone",
      "exec_args": ["-a", "vpp", "-t", "saivpp"],
      "lock": "saivpp",
      "ports": [0, 1, 2, 3, 4, 5, 6, 7],
      "traffic_filters": ["mld"],
      "expected_gaps": {
        "test_poc_l3_vrf_default_admin_state": "Get of a never-set virtual router attribute returns SAI_STATUS_ITEM_NOT_FOUND",
        "test_poc_l2_vlan_invalid_id_rejected[0]": "Target accepts VLAN ID 0",
        "test_poc_l2_vlan_invalid_id_rejected[4095]": "Target accepts VLAN ID 4095",
        "test_poc_l2_vlan_invalid_id_rejected[4096]": "Target accepts VLAN ID 4096",
        "test_poc_l2_vlan_duplicate_rejected": "Duplicate VLAN returns INVALID_PARAMETER, expected ITEM_ALREADY_EXISTS"
      }
    }

## 6. Running tests

    scripts/poc/run_poc.sh <profile> <tests> [pytest options]
    scripts/poc/run_poc.sh saivs poc_$(whoami)
    scripts/poc/run_poc.sh saivpp poc_$(whoami) --traffic
    scripts/poc/run_poc.sh saivs-thrift poc_$(whoami)

Every run takes the profile's lock, runs inside the right container, and writes tests/poc/reports/<profile>-<time>.html
and .xml (JUnit). Never run pytest in a container directly. Keep long runs in tmux.

## 7. MLD filter

On saivpp, VPP itself sends IPv6 multicast listener frames (MLD) on its ports. They would break exact-match packet
checks. The profile lists an mld filter that removes only those frames; every other unexpected frame still fails the
test. The report header shows "mld filter: on|off".

Switch it off for diagnosis (or force it on) with --mld-filter=on|off|auto (default auto = follow the profile):

    scripts/poc/run_poc.sh saivpp poc_$(whoami)/test_poc_l2_traffic.py --traffic --mld-filter off

What it does: the filter makes every PTF poll skip IPv6 MLD messages (ICMPv6 types 130, 131, 132 and 143).
Example of the stray frame seen on saivpp: destination 33:33:00:00:00:16 (all MLDv2 routers), ethertype 0x86DD,
ICMPv6 type 143 (MLDv2 listener report) from the VPP host side. Upstream tests do not load the filter, so
test_l2_basic.py on saivpp is flaky (0 to 5 failures of 7 over 18 runs, 5 to 11 Oct).

## 8. Packet capture

Traffic tests use the pcap_capture fixture: a scapy sniffer on veth1, veth2 and veth3 writes
tests/poc/reports/<test>.pcap. View with: tcpdump -nn -e -r <file>; in Wireshark use vlan.id == 20 for the VLAN 20 tag.

## 9. Expected gaps

A deviation from the SAI specification is not hidden: the test stays and is listed in expected_gaps in the profile
with a reason. The test is then reported as xfail (strict: if it starts to pass, the run fails and the gap is removed).
A "[" in a pattern is literal, so test_poc_l2_vlan_invalid_id_rejected[4095] matches that exact test id.
Add one: python3 add_gap.py <profile> '<test name>' '<reason>'.

Current Track A table (regenerate with python3 gap_table.py before delivery; saivs-thrift evidence appears after
that profile has been run):

| Gap | Target | Test | What the target does (profile reason) | Expected | Evidence |
|---|---|---|---|---|---|
| Duplicate VLAN returns INVALID_PARAMETER | saivpp | `test_poc_l2_vlan_duplicate_rejected` | Duplicate VLAN returns INVALID_PARAMETER, expected ITEM_ALREADY_EXISTS | SAI_STATUS_ITEM_ALREADY_EXISTS | saivpp-1019-105927.xml (xfail) |
| Duplicate VLAN returns INVALID_PARAMETER | saivs | `test_poc_l2_vlan_duplicate_rejected` | Duplicate VLAN returns INVALID_PARAMETER, expected ITEM_ALREADY_EXISTS | SAI_STATUS_ITEM_ALREADY_EXISTS | saivs-1019-110512.xml (xfail) |
| Duplicate VLAN returns INVALID_PARAMETER | saivs-thrift | `test_poc_l2_vlan_duplicate_rejected` | Duplicate VLAN returns INVALID_PARAMETER, expected ITEM_ALREADY_EXISTS | SAI_STATUS_ITEM_ALREADY_EXISTS | no report yet (-) |
| Target accepts VLAN ID 0 | saivpp | `test_poc_l2_vlan_invalid_id_rejected[0]` | Target accepts VLAN ID 0 | rejected (non-success status) | saivpp-1019-105927.xml (xfail) |
| Target accepts VLAN ID 0 | saivs | `test_poc_l2_vlan_invalid_id_rejected[0]` | Target accepts VLAN ID 0 | rejected (non-success status) | saivs-1019-110512.xml (xfail) |
| Target accepts VLAN ID 0 | saivs-thrift | `test_poc_l2_vlan_invalid_id_rejected[0]` | Target accepts VLAN ID 0 | rejected (non-success status) | no report yet (-) |
| Target accepts VLAN ID 4095 | saivpp | `test_poc_l2_vlan_invalid_id_rejected[4095]` | Target accepts VLAN ID 4095 | rejected (non-success status) | saivpp-1019-105927.xml (xfail) |
| Target accepts VLAN ID 4095 | saivs | `test_poc_l2_vlan_invalid_id_rejected[4095]` | Target accepts VLAN ID 4095 | rejected (non-success status) | saivs-1019-110512.xml (xfail) |
| Target accepts VLAN ID 4095 | saivs-thrift | `test_poc_l2_vlan_invalid_id_rejected[4095]` | Target accepts VLAN ID 4095 | rejected (non-success status) | no report yet (-) |
| Target accepts VLAN ID 4096 | saivpp | `test_poc_l2_vlan_invalid_id_rejected[4096]` | Target accepts VLAN ID 4096 | rejected (non-success status) | saivpp-1019-105927.xml (xfail) |
| Target accepts VLAN ID 4096 | saivs | `test_poc_l2_vlan_invalid_id_rejected[4096]` | Target accepts VLAN ID 4096 | rejected (non-success status) | saivs-1019-110512.xml (xfail) |
| Target accepts VLAN ID 4096 | saivs-thrift | `test_poc_l2_vlan_invalid_id_rejected[4096]` | Target accepts VLAN ID 4096 | rejected (non-success status) | no report yet (-) |

## 10. Troubleshooting FAQ

| Symptom | Cause | Fix |
|---|---|---|
| next run fails with "already exists" | an earlier test left objects on the switch | post in chat; the container owner restarts it; find the missing cleanup |
| packets do not arrive on saivpp | VPP interfaces or lcp down | supervisorctl status; vppctl show interface; vppctl show lcp (look, do not change) |
| a test sees an extra IPv6 frame | MLD filter off, or a new frame type | check the header; extend traffic_filters only for frames the target generates |
| mount folder owned by root | folder did not exist before run.sh | create it first, restart the container |
| Docker Hub blocked | network policy | build the image locally (several hours) |
| saivs build fails | build environment | run.sh uses the prebuilt image; note the build error |
| report folder not writable | files created as root in the container | run_poc.sh chowns the folder at the end; chown manually if a run was killed |
| run waits "for the lock" | someone else is running | wait; do not kill their run |
| pcap file missing | the test did not finish or capture is off | rerun the single test with --traffic |
| exec.sh prints "exit code 3" after supervisorctl status | one-shot jobs (dependent-startup, vpp-veth-creator, veth-creator) are EXITED by design | normal; only a long-running process not RUNNING is a problem |
| test_l2_basic.py on saivpp fails with a 33:33:... frame | MLD stray frame; upstream tests do not load the PoC filter | use the PoC tests, which filter MLD |
| exec.sh mangles a quoted argument (sh -c '...') | exec.sh wraps the command in its own bash -c | run the pipe on the host: ./exec.sh ... cmd 2>&1 \| grep ... |
| pcap is only a few hundred bytes | it comes from a failed run (the test stopped at the first check) | rerun the test cleanly and take the pcap from that run |
| a test passes but the run fails with XPASS(strict) | a gap is listed for a test that now passes | remove the gap from the profile |
| fixture error 'function' object is not subscriptable on poc_ports | poc_ports is a factory | use poc_ports(n), not poc_ports[0] |
| git: detected dubious ownership in /opt/sai-poc/SAI-Challenger | repo is owned by another user | git config --global --add safe.directory /opt/sai-poc/SAI-Challenger |

## 11. Adding a new vendor libSAI

Follows the SAI-Challenger porting guide plus these PoC steps:

1. Create the target folder npu/<vendor>/<asic>/<target>/ with the Dockerfile and syncd or saiserver configuration, as
   described in the porting guide.
2. Create a testbed file in testbeds/ that maps the platform ports to the dataplane (veth, NIC or traffic generator).
3. Create tests/poc/targets/<name>.json with the testbed, exec options, lock name and usable ports; leave
   expected_gaps empty.
4. Run the upstream baseline (test_l2_basic.py), then the PoC Level 1 suite through run_poc.sh.
5. Record every confirmed deviation from the SAI specification in expected_gaps with its reason; correct any failure
   caused by the test itself.
6. Run the Level 2 traffic tests if the target forwards packets; add traffic_filters only for frames the target itself
   generates.

Hardware targets additionally need SAI-Challenger client-server mode, physical ports and a traffic source (PTF on
network interfaces, or snappi/ixia-c), as described in the client-server mode guide.
