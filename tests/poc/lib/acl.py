"""
lib/acl.py - small helper library for ACL tests (NOT a test, not run on its own).

Tests import it:   from lib.acl import AclHelper
It only wraps calls the existing ut/test_acl_ut.py already uses:
    npu.create(SaiObjType.X, [attr, value, ...], do_assert=False) -> (status, oid)
    npu.remove(oid)  /  npu.set(oid, [attr, value])

Why: one place that creates/binds/cleans ACL objects, so every test cleans up after
itself even when a create fails (this avoids the cascade seen in test_remove_entries /
test_remove_table).

Typical use:
    acl = AclHelper(npu)
    table = acl.create_table(fields=["IP_PROTOCOL", "L4_DST_PORT"])
    st, entry = acl.add_entry(table, 10, "SAI_PACKET_ACTION_DROP",
                              [("IP_PROTOCOL", "6", "0xff"), ("L4_DST_PORT", "80", "0xffff")])
    acl.bind_to_port(table, npu.port_oids[0])
    ...
    acl.cleanup()          # unbinds, removes entries, then table - always safe to call
"""
from saichallenger.common.sai_data import SaiObjType

OK = "SAI_STATUS_SUCCESS"


class AclHelper:
    def __init__(self, npu):
        self.npu = npu
        self.entries = []      # entry oids created successfully
        self.tables = []       # table oids
        self.bindings = []     # (port_oid, attr) that must be unbound
        self.log = []          # (what, status) - handy to print in test output

    # ---- table ---------------------------------------------------------
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
        self.log.append(("create_table %s" % fields, OK))
        return oid

    # ---- entry ---------------------------------------------------------
    def add_entry(self, table, priority, action, match):
        """match: list of (FIELD_NAME, value, mask). Returns (status, oid).
        A failed create is NOT added to the cleanup list, so it cannot break cleanup."""
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

    # ---- bind / unbind -------------------------------------------------
    def bind_to_port(self, table, port_oid, attr="SAI_PORT_ATTR_INGRESS_ACL"):
        self.npu.set(port_oid, [attr, table])
        self.bindings.append((port_oid, attr))
        self.log.append(("bind port %s" % port_oid, OK))

    def unbind_all(self):
        while self.bindings:
            port_oid, attr = self.bindings.pop()
            self.npu.set(port_oid, [attr, "oid:0x0"])

    # ---- cleanup -------------------------------------------------------
    def cleanup(self):
        """Unbind, remove entries, remove tables. Never raises; returns list of problems."""
        problems = []

        def attempt(label, fn):
            try:
                fn()
            except Exception as e:                      # noqa: BLE001 - cleanup must go on
                problems.append("%s: %s" % (label, str(e)[:150]))

        self.unbind_all_safe(attempt)
        for oid in reversed(self.entries):
            attempt("remove entry %s" % oid, lambda o=oid: self.npu.remove(o))
        self.entries = []
        for oid in reversed(self.tables):
            attempt("remove table %s" % oid, lambda o=oid: self.npu.remove(o))
        self.tables = []
        return problems

    def unbind_all_safe(self, attempt):
        for port_oid, attr in reversed(self.bindings):
            attempt("unbind %s" % port_oid, lambda p=port_oid, a=attr: self.npu.set(p, [a, "oid:0x0"]))
        self.bindings = []

    def summary(self):
        return "\n".join("  %-40s %s" % (w, s) for w, s in self.log)
