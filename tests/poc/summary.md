# Test summary

## Reports

| Column | Report | Result |
|---|---|---|
| saivs#1 | saivs-1019-042627.xml | 16 passed, 0 failed, 1 xfail, 7 skipped |
| saivs#2 | saivs-1019-042654.xml | 16 passed, 0 failed, 1 xfail, 7 skipped |
| saivpp#1 | saivpp-1019-042714.xml | 21 passed, 2 failed, 1 xfail, 0 skipped |
| saivpp#2 | saivpp-1019-042740.xml | 21 passed, 2 failed, 1 xfail, 0 skipped |

## Results side by side

| Test | saivs#1 | saivs#2 | saivpp#1 | saivpp#2 |
|---|---|---|---|---|
| **test_poc_acl.py** | | | | |
| test_poc_acl_table_create_remove | PASS (22.89s) | PASS (16.35s) | PASS (11.70s) | PASS (9.97s) |
| test_poc_acl_table_readback | PASS (0.04s) | PASS (0.04s) | PASS (0.04s) | PASS (0.04s) |
| test_poc_acl_entry_drop_create_remove | PASS (0.05s) | PASS (0.05s) | PASS (0.05s) | PASS (0.05s) |
| test_poc_acl_entry_priority_readback | PASS (0.06s) | PASS (0.06s) | PASS (0.07s) | PASS (0.06s) |
| test_poc_acl_bind_port | PASS (0.07s) | PASS (0.07s) | PASS (0.07s) | PASS (0.07s) |
| test_poc_acl_match_src_ipv6 | PASS (0.05s) | PASS (0.05s) | FAIL (0.04s) | FAIL (0.04s) |
| test_poc_acl_match_dst_mac | PASS (0.05s) | PASS (0.05s) | PASS (7.60s) | PASS (11.83s) |
| test_poc_acl_table_group_create_remove | PASS (0.07s) | PASS (0.07s) | PASS (0.07s) | PASS (0.07s) |
| test_poc_acl_entry_unknown_field_rejected | XFAIL (0.05s) | XFAIL (0.05s) | XFAIL (0.05s) | XFAIL (0.05s) |
| TestPocAclDrop::test_poc_acl_drop_01_l2_configured | SKIP (0.00s) | SKIP (0.00s) | PASS (0.18s) | PASS (0.18s) |
| TestPocAclDrop::test_poc_acl_drop_02_baseline_both_ports_forward | SKIP (0.00s) | SKIP (0.00s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_03_table_created | PASS (0.01s) | PASS (0.01s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_04_entries_created | PASS (0.03s) | PASS (0.03s) | PASS (0.03s) | PASS (0.03s) |
| TestPocAclDrop::test_poc_acl_drop_05_bound_to_ingress_port | PASS (0.04s) | PASS (0.04s) | PASS (0.04s) | PASS (0.04s) |
| TestPocAclDrop::test_poc_acl_drop_06_drop_port_is_dropped | SKIP (0.00s) | SKIP (0.00s) | PASS (2.00s) | PASS (2.01s) |
| TestPocAclDrop::test_poc_acl_drop_07_fwd_port_is_forwarded | SKIP (0.00s) | SKIP (0.00s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_08_unmatched_port_informational | SKIP (0.00s) | SKIP (0.00s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_09_port_acl_readback | PASS (0.01s) | PASS (0.01s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_10_unbound | PASS (0.01s) | PASS (0.01s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_11_entries_removed | PASS (0.03s) | PASS (0.03s) | PASS (0.03s) | PASS (0.03s) |
| TestPocAclDrop::test_poc_acl_drop_12_table_removed | PASS (0.04s) | PASS (0.04s) | PASS (0.04s) | PASS (0.04s) |
| TestPocAclDrop::test_poc_acl_drop_13_forwarding_after_acl_removed | SKIP (0.00s) | SKIP (0.00s) | PASS (0.01s) | PASS (0.01s) |
| TestPocAclDrop::test_poc_acl_drop_14_l2_removed_and_defaults_restored | SKIP (0.00s) | SKIP (0.00s) | PASS (0.11s) | PASS (0.11s) |
| **test_poc_counters.py** | | | | |
| test_poc_counters_clear_read | PASS (0.04s) | PASS (0.04s) | FAIL (0.19s) | FAIL (0.18s) |

## Gaps (xfail reasons)

| Target | Test | Reason | Source |
|---|---|---|---|
| saivs | test_poc_acl_entry_unknown_field_rejected | target accepted an ACL entry with a field its table lacks | report |
| saivpp | test_poc_acl_entry_unknown_field_rejected | target accepted an ACL entry with a field its table lacks | report |

## Profile checks

- saivs: xfail tests found but no targets/saivs.json was found, so reasons come from the report.
- NEW GAP saivs: test_poc_acl_entry_unknown_field_rejected is xfail but not in the saivs profile. Add it to targets/saivs.json.
- NEW GAP saivpp: test_poc_acl_entry_unknown_field_rejected is xfail but not in the saivpp profile. Add it to targets/saivpp.json.
- saivpp: test_poc_acl_match_src_ipv6 is declared as a gap but FAILED (not xfail). The xfail marker is not being applied.

## Run consistency

- saivs: same result in all 2 runs.
- saivpp: same result in all 2 runs.

## Failures

- `saivpp#1` `test_poc_acl.py::test_poc_acl_match_src_ipv6` - AssertionError: SRC_IPV6 entry create failed: SAI_STATUS_FAILURE
- `saivpp#1` `test_poc_counters.py::test_poc_counters_clear_read` - AssertionError: counters not zero after clear: {'SAI_PORT_STAT_IF_IN_OCTETS': 0, 'SAI_PORT_STAT_IF_OUT_OCTETS': 150}
- `saivpp#2` `test_poc_acl.py::test_poc_acl_match_src_ipv6` - AssertionError: SRC_IPV6 entry create failed: SAI_STATUS_FAILURE
- `saivpp#2` `test_poc_counters.py::test_poc_counters_clear_read` - AssertionError: counters not zero after clear: {'SAI_PORT_STAT_IF_IN_OCTETS': 0, 'SAI_PORT_STAT_IF_OUT_OCTETS': 150}
