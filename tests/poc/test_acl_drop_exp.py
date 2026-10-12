
import os
import sys
import pytest

from saichallenger.common.sai_data import SaiObjType
from ptf.testutils import (
    simple_tcp_packet,
    send_packet,
    verify_packet,
    verify_no_packet,
)

# The host's tests/poc folder is mounted as tests/poc_shubham in Docker.
# Therefore, this file's directory contains the mounted lib/acl.py.
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from lib.acl import AclHelper


# PTF dataplane ports, not TCP destination ports.
IN_PORT = 0
OUT_PORT = 1

VLAN_ID = "10"
SRC_MAC = "00:11:11:11:11:11"
DST_MAC = "00:22:22:22:22:22"

RESULT = {}


def make_tcp(dport):
    return simple_tcp_packet(
        eth_src=SRC_MAC,
        eth_dst=DST_MAC,
        ip_src="20.0.0.2",
        ip_dst="192.168.1.10",
        tcp_sport=1234,
        tcp_dport=dport,
        ip_ttl=64,
    )


def send_and_verify_forward(dataplane, dport):
    pkt = make_tcp(dport)
    send_packet(dataplane, IN_PORT, pkt)
    verify_packet(dataplane, pkt, OUT_PORT, timeout=2)


def send_and_verify_drop(dataplane, dport):
    pkt = make_tcp(dport)
    send_packet(dataplane, IN_PORT, pkt)
    verify_no_packet(dataplane, pkt, OUT_PORT, timeout=2)


def test_acl_drop_tcp80_forward_tcp81(npu, dataplane):
    if not npu.run_traffic:
        pytest.skip("Dataplane traffic is disabled for this testbed")

    if len(npu.port_oids) < 2 or len(npu.dot1q_bp_oids) < 2:
        pytest.skip("This test requires at least two ports")

    ingress_port = npu.port_oids[IN_PORT]
    egress_port = npu.port_oids[OUT_PORT]

    ingress_bp = npu.dot1q_bp_oids[IN_PORT]
    egress_bp = npu.dot1q_bp_oids[OUT_PORT]

    vlan_oid = None
    vlan_members = []
    default_vlan_members_removed = []
    fdb_macs_created = []

    acl = AclHelper(npu)
    acl_group_oid = None
    acl_group_member_oid = None
    group_bound = False

    try:
        # ---------------------------------------------------------
        # 1. Configure L2 forwarding.
        # ---------------------------------------------------------
        vlan_oid = npu.create(
            SaiObjType.VLAN,
            ["SAI_VLAN_ATTR_VLAN_ID", VLAN_ID],
        )

        for port, bridge_port in (
            (ingress_port, ingress_bp),
            (egress_port, egress_bp),
        ):
            npu.remove_vlan_member(
                npu.default_vlan_oid,
                bridge_port,
            )
            default_vlan_members_removed.append(bridge_port)

            member_oid = npu.create_vlan_member(
                vlan_oid,
                bridge_port,
                "SAI_VLAN_TAGGING_MODE_UNTAGGED",
            )
            vlan_members.append(member_oid)

            npu.set(
                port,
                ["SAI_PORT_ATTR_PORT_VLAN_ID", VLAN_ID],
            )

        npu.create_fdb(vlan_oid, SRC_MAC, ingress_bp)
        fdb_macs_created.append(SRC_MAC)

        npu.create_fdb(vlan_oid, DST_MAC, egress_bp)
        fdb_macs_created.append(DST_MAC)

        # ---------------------------------------------------------
        # 2. Verify the actual forwarding port BEFORE applying ACL.
        #    Your current logs show this is PTF port 1.
        # ---------------------------------------------------------
        send_and_verify_forward(dataplane, 80)
        send_and_verify_forward(dataplane, 81)

        RESULT["baseline_forwarding"] = True
        RESULT["expected_egress_ptf_port"] = OUT_PORT
        print("RESULT baseline_forwarding=True")
        print("RESULT expected_egress_ptf_port=%s" % OUT_PORT)

        # ---------------------------------------------------------
        # 3. Create ACL table and entries.
        # ---------------------------------------------------------
        table_oid = acl.create_table(
            fields=["IP_PROTOCOL", "L4_DST_PORT"],
        )

        status, _ = acl.add_entry(
            table_oid,
            200,
            "SAI_PACKET_ACTION_DROP",
            [
                ("IP_PROTOCOL", "6", "0xff"),
                ("L4_DST_PORT", "80", "0xffff"),
            ],
        )

        print("RESULT add_tcp80_drop_entry=%s" % status)
        assert status == "SAI_STATUS_SUCCESS", (
            "Failed to create TCP/80 DROP entry"
        )

        status, _ = acl.add_entry(
            table_oid,
            100,
            "SAI_PACKET_ACTION_FORWARD",
            [
                ("IP_PROTOCOL", "6", "0xff"),
                ("L4_DST_PORT", "81", "0xffff"),
            ],
        )

        print("RESULT add_tcp81_forward_entry=%s" % status)
        assert status == "SAI_STATUS_SUCCESS", (
            "Failed to create TCP/81 FORWARD entry"
        )

        # ---------------------------------------------------------
        # 4. Create an ingress ACL table group.
        # ---------------------------------------------------------
        acl_group_oid = npu.create(
            SaiObjType.ACL_TABLE_GROUP,
            [
                "SAI_ACL_TABLE_GROUP_ATTR_ACL_STAGE",
                "SAI_ACL_STAGE_INGRESS",

                "SAI_ACL_TABLE_GROUP_ATTR_ACL_BIND_POINT_TYPE_LIST",
                "1:SAI_ACL_BIND_POINT_TYPE_PORT",

                "SAI_ACL_TABLE_GROUP_ATTR_TYPE",
                "SAI_ACL_TABLE_GROUP_TYPE_PARALLEL",
            ],
        )

        print("RESULT acl_group_oid=%s" % acl_group_oid)

        # Attach the ACL table to the group.
        acl_group_member_oid = npu.create(
            SaiObjType.ACL_TABLE_GROUP_MEMBER,
            [
                "SAI_ACL_TABLE_GROUP_MEMBER_ATTR_ACL_TABLE_GROUP_ID",
                acl_group_oid,

                "SAI_ACL_TABLE_GROUP_MEMBER_ATTR_ACL_TABLE_ID",
                table_oid,

                "SAI_ACL_TABLE_GROUP_MEMBER_ATTR_PRIORITY",
                "100",
            ],
        )

        print(
            "RESULT acl_group_member_oid=%s"
            % acl_group_member_oid
        )

        # Bind the group to the actual ingress port: PTF port 0.
        bind_status = npu.set(
            ingress_port,
            ["SAI_PORT_ATTR_INGRESS_ACL", acl_group_oid],
        )
        group_bound = True

        print("RESULT acl_group_bind_status=%s" % bind_status)

        # ---------------------------------------------------------
        # 5. Verify ACL behavior using the observed egress port.
        # ---------------------------------------------------------
        send_and_verify_drop(dataplane, 80)

        RESULT["tcp80_dropped"] = True
        print("RESULT tcp80_dropped=True")

        send_and_verify_forward(dataplane, 81)

        RESULT["tcp81_forwarded"] = True
        print("RESULT tcp81_forwarded=True")

    finally:
        # ---------------------------------------------------------
        # 6. Unbind the ACL group before removing its objects.
        # ---------------------------------------------------------
        if group_bound:
            try:
                npu.set(
                    ingress_port,
                    ["SAI_PORT_ATTR_INGRESS_ACL", "oid:0x0"],
                )
                print("RESULT acl_group_unbound=True")
            except Exception as exc:
                print("ACL group unbind warning: %s" % exc)

        if acl_group_member_oid is not None:
            try:
                npu.remove(acl_group_member_oid)
            except Exception as exc:
                print("ACL group member cleanup warning: %s" % exc)

        if acl_group_oid is not None:
            try:
                npu.remove(acl_group_oid)
            except Exception as exc:
                print("ACL group cleanup warning: %s" % exc)

        # The group is removed; now clean up ACL entries and table.
        try:
            acl_problems = acl.cleanup()
            print("RESULT acl_cleanup_problems=%s" % acl_problems)
        except Exception as exc:
            print("ACL cleanup warning: %s" % exc)

        # Remove only FDB entries successfully created by this test.
        if vlan_oid is not None:
            for mac in reversed(fdb_macs_created):
                try:
                    npu.remove_fdb(vlan_oid, mac)
                except Exception as exc:
                    print("FDB cleanup warning for %s: %s" % (mac, exc))

        for member_oid in reversed(vlan_members):
            try:
                npu.remove(member_oid)
            except Exception as exc:
                print("VLAN member cleanup warning: %s" % exc)

        for bridge_port in default_vlan_members_removed:
            try:
                npu.create_vlan_member(
                    npu.default_vlan_oid,
                    bridge_port,
                    "SAI_VLAN_TAGGING_MODE_UNTAGGED",
                )
            except Exception as exc:
                print("Default VLAN restoration warning: %s" % exc)

        for port in (ingress_port, egress_port):
            try:
                npu.set(
                    port,
                    [
                        "SAI_PORT_ATTR_PORT_VLAN_ID",
                        npu.default_vlan_id,
                    ],
                )
            except Exception as exc:
                print("Port PVID restoration warning: %s" % exc)

        if vlan_oid is not None:
            try:
                npu.remove(vlan_oid)
            except Exception as exc:
                print("VLAN cleanup warning: %s" % exc)

        print("RESULT final=%s" % RESULT)
