# Test summary

## saivpp-1005-1116.xml (saivpp)
**6 passed, 1 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_l2_basic.py | 6 | 1 | 0 |

Failures:

- `test_l2_basic.py::test_l2_access_to_access_vlan` - AssertionError: A packet was received on device 0, port 0, but we expected no packets.

## saivpp-1007-0855.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1007-0901.xml (saivpp)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_entry.py | 2 | 0 | 0 |

## saivpp-1007-0902.xml (saivpp)
**0 passed, 1 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_stats.py | 0 | 1 | 0 |

Failures:

- `test_stats.py::test_stats` - AssertionError: SAI_PORT_STAT_IF_OUT_OCTETS is not 0

## saivpp-1007-0908.xml (saivpp)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_entry.py | 2 | 0 | 0 |

## saivpp-1007-0910.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1007-0924.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1007-111715.xml (saivpp)
**0 passed, 1 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_stats.py | 0 | 1 | 0 |

Failures:

- `test_stats.py::test_stats` - AssertionError: SAI_PORT_STAT_IF_OUT_OCTETS is not 0

## saivpp-1008-063447.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1008-063639.xml (saivpp)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_table.py | 2 | 0 | 0 |

## saivpp-1008-063727.xml (saivpp)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_entry.py | 2 | 0 | 0 |

## saivpp-1008-063814.xml (saivpp)
**1 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_stats.py | 1 | 0 | 0 |

## saivpp-1008-090847.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1008-101159.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1009-164211.xml (saivpp)
**6 passed, 4 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 6 | 4 | 0 |

Failures:

- `ut/test_acl_ut.py::test_create_entry[match5-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: ETHER_TYPE, SRC_IPV6, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_create_entry[match6-SAI_PACKET_ACTION_FORWARD-9987]` - Failed to create ACL entry (fields: DST_MAC, ETHER_TYPE, IP_PROTOCOL) -> status FAILURE
- `ut/test_acl_ut.py::test_remove_entries` - remove(SAI_OBJECT_TYPE_ACL_ENTRY:oid:0x800000000030b) --> SAI_STATUS_FAILURE
- `ut/test_acl_ut.py::test_remove_table` - remove(SAI_OBJECT_TYPE_ACL_TABLE:oid:0x7000000000309) --> SAI_STATUS_OBJECT_IN_USE

## saivpp-1009-173554.xml (saivpp)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_table.py | 2 | 0 | 0 |

## saivpp-1009-173634.xml (saivpp)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_entry.py | 2 | 0 | 0 |

## saivpp-1009-173727.xml (saivpp)
**0 passed, 1 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_stats.py | 0 | 1 | 0 |

Failures:

- `test_stats.py::test_stats` - AssertionError: SAI_PORT_STAT_IF_OUT_OCTETS is not 0

## saivpp-1009-173758.xml (saivpp)
**1 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_stats.py | 1 | 0 | 0 |

## saivpp-1009-184235.xml (saivpp)
**0 passed, 0 failed, 1 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| poc_shubham/test_acl_drop_exp.py | 0 | 0 | 1 |

## saivpp-1009-185035.xml (saivpp)
**0 passed, 1 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| poc_shubham/test_acl_drop_exp.py | 0 | 1 | 0 |

Failures:

- `poc_shubham/test_acl_drop_exp.py::test_acl_drop_tcp_80_forward_tcp_81` - AssertionError: SAI "create" operation failure!

## saivs-1005-1115.xml (saivs)
**7 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_l2_basic.py | 7 | 0 | 0 |

## saivs-1005-1119.xml (saivs)
**0 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|

## saivs-1005-1121.xml (saivs)
**7 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_l2_basic.py | 7 | 0 | 0 |

## saivs-1005-1537.xml (saivs)
**10 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 10 | 0 | 0 |

## saivs-1006-0639.xml (saivs)
**10 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| ut/test_acl_ut.py | 10 | 0 | 0 |

## saivs-1006-0641.xml (saivs)
**2 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_table.py | 2 | 0 | 0 |

## saivs-1006-0642.xml (saivs)
**1 passed, 0 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| test_stats.py | 1 | 0 | 0 |

## saivs-1006-1602.xml (saivs)
**0 passed, 2 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_entry.py | 0 | 2 | 0 |

Failures:

- `api/test_acl_entry.py::test_acl_entry_create` - failed on setup with "AssertionError: SyncD has not started yet..."
- `api/test_acl_entry.py::test_acl_entry_remove` - failed on setup with "AssertionError: SyncD has not started yet..."

## saivs-1006-1610.xml (saivs)
**0 passed, 2 failed, 0 skipped**

| Test file | Passed | Failed | Skipped |
|---|---:|---:|---:|
| api/test_acl_entry.py | 0 | 2 | 0 |

Failures:

- `api/test_acl_entry.py::test_acl_entry_create` - failed on setup with "AssertionError: SyncD has not started yet..."
- `api/test_acl_entry.py::test_acl_entry_remove` - failed on setup with "AssertionError: SyncD has not started yet..."
