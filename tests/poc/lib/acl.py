"""
lib/acl.py - helper library for ACL tests (a library, not a test; nothing to run).

Tests import it:   from lib.acl import AclHelper

Wraps only calls the existing ut/test_acl_ut.py already uses:
    npu.create(SaiObjType.X, [attr, value, ...])                    -> oid
    npu.create(SaiObjType.ACL_ENTRY, [...], do_assert=False)        -> (status, oid)
    npu.set(oid, [attr, value]) / npu.remove(oid)

Every object it creates is remembered, so cleanup() removes them in REVERSE order of
dependency:  unbind ports -> entries -> group members -> groups -> tables.
cleanup() is safe to call any number of times and never raises.

Single-object helpers (unbind_port, remove_entry, remove_group, remove_table) are strict:
they raise if SAI refuses, so a test can assert on them.
"""
from saichallenger.common.sai_data import SaiObjType

OK = "SAI_STATUS_SUCCESS"
UNBOUND = "oid:0x0"
PORT_ACL_ATTR = "SAI_PORT_ATTR_INGRESS_ACL"


class AclHelper:
    def __init__(self, npu):
        self.npu = npu
        self.tables, self.entries = [], []
        self.groups, self.members = [], []
        self.bindings = []          # (port_oid, attr) currently bound by us
        self.log = []               # (what, status) - handy to print

    # ---- create --------------------------------------------------------
    def create_table(self, fields, stage="INGRESS", bind_point="PORT"):
        """fields: names without prefix, e.g. ["IP_PROTOCOL", "L4_DST_PORT"]."""
        attrs = [
            "SAI_ACL_TABLE_ATTR_ACL_STAGE", "SAI_ACL_STAGE_%s" % stage,
            "SAI_ACL_TABLE_ATTR_ACL_BIND_POINT_TYPE_LIST", "1:SAI_ACL_BIND_POINT_TYPE_%s" % bind_point,
            "SAI_ACL_TABLE_ATTR_ACL_ACTION_TYPE_LIST", "1:SAI_ACL_ACTION_TYPE_PACKET_ACTION",
        ]
        for f in fields:
            attrs += ["SAI_ACL_TABLE_ATTR_FIELD_%s" % f, "true"]
        oid = self.npu.create(SaiObjType.ACL_TABLE, attrs)
        self.tables.append(oid)
        self.log.append(("create_table %s" % list(fields), OK))
        return oid

    def add_entry(self, table, priority, action, match):
        """match: list of (FIELD_NAME, value, mask). Returns (status, oid).
        A failed create is NOT remembered, so it can never break cleanup."""
        attrs = [
            "SAI_ACL_ENTRY_ATTR_TABLE_ID", table,
            "SAI_ACL_ENTRY_ATTR_PRIORITY", str(priority),
            "SAI_ACL_ENTRY_ATTR_ADMIN_STATE", "true",
            "SAI_ACL_ENTRY_ATTR_ACTION_PACKET_ACTION", action,
        ]
        for name, value, mask in match:
            attrs += ["SAI_ACL_ENTRY_ATTR_FIELD_%s" % name, "%s&mask:%s" % (value, mask)]
        status, oid = self.npu.create(SaiObjType.ACL_ENTRY, attrs, do_assert=False)
        self.log.append(("add_entry %s" % [m[0] for m in match], status))
        if status == OK:
            self.entries.append(oid)
        return status, oid

    def create_group(self, table, stage="INGRESS", bind_point="PORT", priority="100"):
        """Optional: table group + one member holding `table`. Returns the group oid."""
        group = self.npu.create(SaiObjType.ACL_TABLE_GROUP, [
            "SAI_ACL_TABLE_GROUP_ATTR_ACL_STAGE", "SAI_ACL_STAGE_%s" % stage,
            "SAI_ACL_TABLE_GROUP_ATTR_ACL_BIND_POINT_TYPE_LIST", "1:SAI_ACL_BIND_POINT_TYPE_%s" % bind_point,
            "SAI_ACL_TABLE_GROUP_ATTR_TYPE", "SAI_ACL_TABLE_GROUP_TYPE_PARALLEL",
        ])
        self.groups.append(group)
        member = self.npu.create(SaiObjType.ACL_TABLE_GROUP_MEMBER, [
            "SAI_ACL_TABLE_GROUP_MEMBER_ATTR_ACL_TABLE_GROUP_ID", group,
            "SAI_ACL_TABLE_GROUP_MEMBER_ATTR_ACL_TABLE_ID", table,
            "SAI_ACL_TABLE_GROUP_MEMBER_ATTR_PRIORITY", str(priority),
        ])
        self.members.append(member)
        self.log.append(("create_group", OK))
        return group

    # ---- bind / unbind -------------------------------------------------
    def bind_to_port(self, acl_oid, port_oid, attr=PORT_ACL_ATTR):
        """acl_oid: a table oid, or a group oid from create_group()."""
        self.npu.set(port_oid, [attr, acl_oid])
        self.bindings.append((port_oid, attr))
        self.log.append(("bind port %s" % port_oid, OK))

    def unbind_port(self, port_oid, attr=PORT_ACL_ATTR):
        self.npu.set(port_oid, [attr, UNBOUND])
        self.bindings = [b for b in self.bindings if b != (port_oid, attr)]
        self.log.append(("unbind port %s" % port_oid, OK))

    # ---- strict single-object removal (raise on failure) ---------------
    def remove_entry(self, oid):
        self.npu.remove(oid)
        self.entries.remove(oid)
        self.log.append(("remove_entry", OK))

    def remove_group(self):
        """Remove all group members, then all groups."""
        for oid in list(reversed(self.members)):
            self.npu.remove(oid)
            self.members.remove(oid)
        for oid in list(reversed(self.groups)):
            self.npu.remove(oid)
            self.groups.remove(oid)
        self.log.append(("remove_group", OK))

    def remove_table(self, oid):
        self.npu.remove(oid)
        self.tables.remove(oid)
        self.log.append(("remove_table", OK))

    # ---- lenient full cleanup ------------------------------------------
    def cleanup(self):
        """Undo everything still alive, in reverse dependency order. Never raises;
        returns a list of problem strings (empty list = clean)."""
        problems = []

        def attempt(label, fn):
            try:
                fn()
            except Exception as e:                       # noqa: BLE001
                problems.append("%s: %s" % (label, str(e)[:150]))

        for port_oid, attr in reversed(self.bindings):
            attempt("unbind %s" % port_oid, lambda p=port_oid, a=attr: self.npu.set(p, [a, UNBOUND]))
        for oid in reversed(self.entries):
            attempt("remove entry %s" % oid, lambda o=oid: self.npu.remove(o))
        for oid in reversed(self.members):
            attempt("remove group member %s" % oid, lambda o=oid: self.npu.remove(o))
        for oid in reversed(self.groups):
            attempt("remove group %s" % oid, lambda o=oid: self.npu.remove(o))
        for oid in reversed(self.tables):
            attempt("remove table %s" % oid, lambda o=oid: self.npu.remove(o))
        self.bindings, self.entries, self.members, self.groups, self.tables = [], [], [], [], []
        return problems

    def summary(self):
        return "\n".join("  %-40s %s" % (w, s) for w, s in self.log)
