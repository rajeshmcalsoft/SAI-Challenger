"""lib/acl.py - helper library for ACL tests (not a test).

    from lib.acl import AclHelper

Wraps npu.create / set / remove for ACL tables, entries, groups and port binding, and
remembers every object so cleanup() removes them in reverse order (safe to call any
number of times, never raises). Single-object helpers raise if SAI refuses.
"""
from saichallenger.common.sai_data import SaiObjType

from .params import load

# Defaults come from data/ACL.json ("acl" section); only the SAI status name stays here.
_ACL = load()["acl"]
OK = "SAI_STATUS_SUCCESS"
UNBOUND = _ACL["unbound_oid"]
PORT_ACL_ATTR = _ACL["port_acl_attr"]


class AclHelper:
    def __init__(self, npu):
        self.npu = npu
        self.tables, self.entries = [], []
        self.groups, self.members = [], []
        self.bindings = []          # (port_oid, attr) currently bound by us
        self.log = []               # (what, status) - handy to print

    # ---- create --------------------------------------------------------
    def create_table(self, fields, stage=_ACL["stage"], bind_point=_ACL["bind_point"]):
        """fields: names without prefix, e.g. ["IP_PROTOCOL", "L4_DST_PORT"]."""
        attrs = [
            "SAI_ACL_TABLE_ATTR_ACL_STAGE", f"SAI_ACL_STAGE_{stage}",
            "SAI_ACL_TABLE_ATTR_ACL_BIND_POINT_TYPE_LIST", f"1:SAI_ACL_BIND_POINT_TYPE_{bind_point}",
            "SAI_ACL_TABLE_ATTR_ACL_ACTION_TYPE_LIST", f"1:{_ACL['action_type']}",
        ]
        for f in fields:
            attrs += [f"SAI_ACL_TABLE_ATTR_FIELD_{f}", "true"]
        oid = self.npu.create(SaiObjType.ACL_TABLE, attrs)
        self.tables.append(oid)
        self.log.append((f"create_table {list(fields)}", OK))
        return oid

    def add_entry(self, table, priority, action, match):
        """match: list of (FIELD_NAME, value, mask). Returns (status, oid).
        A failed create is NOT remembered, so it can never break cleanup."""
        attrs = [
            "SAI_ACL_ENTRY_ATTR_TABLE_ID", table,
            "SAI_ACL_ENTRY_ATTR_PRIORITY", str(priority),
            "SAI_ACL_ENTRY_ATTR_ADMIN_STATE", _ACL["entry_admin_state"],
            "SAI_ACL_ENTRY_ATTR_ACTION_PACKET_ACTION", action,
        ]
        for name, value, mask in match:
            attrs += [f"SAI_ACL_ENTRY_ATTR_FIELD_{name}", f"{value}&mask:{mask}"]
        status, oid = self.npu.create(SaiObjType.ACL_ENTRY, attrs, do_assert=False)
        self.log.append(("add_entry %s" % [m[0] for m in match], status))
        if status == OK:
            self.entries.append(oid)
        return status, oid

    def create_group(self, table, stage=_ACL["stage"], bind_point=_ACL["bind_point"],
                     priority=_ACL["group_member_priority"]):
        """Optional: table group + one member holding `table`. Returns the group oid."""
        group = self.npu.create(SaiObjType.ACL_TABLE_GROUP, [
            "SAI_ACL_TABLE_GROUP_ATTR_ACL_STAGE", f"SAI_ACL_STAGE_{stage}",
            "SAI_ACL_TABLE_GROUP_ATTR_ACL_BIND_POINT_TYPE_LIST", f"1:SAI_ACL_BIND_POINT_TYPE_{bind_point}",
            "SAI_ACL_TABLE_GROUP_ATTR_TYPE", _ACL["group_type"],
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
        status = self.npu.set(port_oid, [attr, acl_oid])
        self.bindings.append((port_oid, attr))
        self.log.append((f"bind port {port_oid}", OK))
        return status

    def unbind_port(self, port_oid, attr=PORT_ACL_ATTR):
        status = self.npu.set(port_oid, [attr, UNBOUND])
        self.bindings = [b for b in self.bindings if b != (port_oid, attr)]
        self.log.append((f"unbind port {port_oid}", OK))
        return status

    # ---- strict single-object removal (raise on failure) ---------------
    def remove_entry(self, oid):
        status = self.npu.remove(oid)
        self.entries.remove(oid)
        self.log.append(("remove_entry", OK))
        return status

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
        status = self.npu.remove(oid)
        self.tables.remove(oid)
        self.log.append(("remove_table", OK))
        return status

    # ---- lenient full cleanup ------------------------------------------
    def cleanup(self):
        """Undo everything still alive, in reverse dependency order. Never raises;
        returns a list of problem strings (empty list = clean)."""
        problems = []

        def attempt(label, fn):
            try:
                fn()
            except Exception as e:                       # noqa: BLE001
                problems.append(f"{label}: {str(e)[:150]}")

        for port_oid, attr in reversed(self.bindings):
            attempt(f"unbind {port_oid}", lambda p=port_oid, a=attr: self.npu.set(p, [a, UNBOUND]))
        for oid in reversed(self.entries):
            attempt(f"remove entry {oid}", lambda o=oid: self.npu.remove(o))
        for oid in reversed(self.members):
            attempt(f"remove group member {oid}", lambda o=oid: self.npu.remove(o))
        for oid in reversed(self.groups):
            attempt(f"remove group {oid}", lambda o=oid: self.npu.remove(o))
        for oid in reversed(self.tables):
            attempt(f"remove table {oid}", lambda o=oid: self.npu.remove(o))
        self.bindings, self.entries, self.members, self.groups, self.tables = [], [], [], [], []
        return problems

    def summary(self):
        return "\n".join("  %-40s %s" % (w, s) for w, s in self.log)
