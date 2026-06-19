import contextlib
import logging
import re
import pytest
from tests.common.platform.interface_utils import get_dpu_npu_ports_from_hwsku
from tests.common.utilities import wait_until
from tests.common.config_reload import config_reload
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.dut_utils import is_virtual_platform

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t0', 't1', 't2', 'lrh', 'urh', 'm0', 'mx', 'm1', 'c0'),
    pytest.mark.device_type('vs')
]


@pytest.fixture(scope="module", autouse="True")
def lldp_setup(duthosts, enum_rand_one_per_hwsku_frontend_hostname, patch_lldpctl, unpatch_lldpctl, localhost):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    patch_lldpctl(localhost, duthost)
    yield
    unpatch_lldpctl(localhost, duthost)


@pytest.fixture(scope="function")
def restart_swss_container(duthosts, enum_rand_one_per_hwsku_frontend_hostname, enum_frontend_asic_index):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    # Check for swss autorestart state
    swss_autorestart_state = "enabled" if "enabled" in duthost.shell("show feature autorestart swss")['stdout'] \
        else "disabled"
    asic = duthost.asic_instance(enum_frontend_asic_index)

    pre_lldpctl_facts = get_num_lldpctl_facts(duthost, enum_frontend_asic_index)
    assert pre_lldpctl_facts != 0, (
        "Cannot get lldp neighbor information. "
        "No LLDP neighbor entries were detected before restarting orchagent. "
        "pre_lldpctl_facts value: {}"
    ).format(pre_lldpctl_facts)

    duthost.shell("sudo systemctl reset-failed")
    duthost.shell("sudo systemctl restart {}".format(asic.get_service_name("swss")))

    # make sure all critical services are up
    assert wait_until(600, 5, 30, duthost.critical_services_fully_started), (
        "Not all critical services are fully started after restarting orchagent. "
    )

    # wait for ports to be up and lldp neighbor information has been received by dut
    assert wait_until(300, 20, 60,
                      lambda: pre_lldpctl_facts == get_num_lldpctl_facts(duthost, enum_frontend_asic_index)), (
        "Cannot get all lldp entries. "
        "Expected LLDP entries: {}\n"
        "Current LLDP entries: {}"
    ).format(
        pre_lldpctl_facts,
        get_num_lldpctl_facts(duthost, enum_frontend_asic_index)
    )

    yield

    duthost.shell(f"sudo config feature autorestart swss {swss_autorestart_state}")


def get_num_lldpctl_facts(duthost, enum_frontend_asic_index):
    internal_port_list = get_dpu_npu_ports_from_hwsku(duthost)
    lldpctl_facts = duthost.lldpctl_facts(
        asic_instance_id=enum_frontend_asic_index,
        skip_interface_pattern_list=["eth0", "Ethernet-BP", "Ethernet-IB"] + internal_port_list)['ansible_facts']
    if not list(lldpctl_facts['lldpctl'].items()):
        return 0
    return len(lldpctl_facts['lldpctl'])


def test_lldp(duthosts, enum_rand_one_per_hwsku_frontend_hostname, localhost,
              collect_techsupport_all_duts, enum_frontend_asic_index, request):
    """ verify the LLDP message on DUT """
    converged = duthosts.tbinfo['topo']['properties'].get('topo_is_multi_vrf', False)
    convergence_info = None
    rev_vrf_map = {}
    if converged:
        convergence_info = duthosts.tbinfo['topo']['properties']['convergence_data']
        for primary, vrflist in convergence_info['convergence_mapping'].items():
            for vrf in vrflist:
                rev_vrf_map[vrf] = primary

    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    config_facts = duthost.asic_instance(
        enum_frontend_asic_index).config_facts(host=duthost.hostname, source="running")['ansible_facts']
    internal_port_list = get_dpu_npu_ports_from_hwsku(duthost)
    lldpctl_facts = duthost.lldpctl_facts(
        asic_instance_id=enum_frontend_asic_index,
        skip_interface_pattern_list=["eth0", "Ethernet-BP", "Ethernet-IB"] + internal_port_list)['ansible_facts']
    if not list(lldpctl_facts['lldpctl'].items()):
        pytest.fail("No LLDP neighbors received (lldpctl_facts are empty)")
    for k, v in list(lldpctl_facts['lldpctl'].items()):
        if converged:
            exp_intf = config_facts['DEVICE_NEIGHBOR'][k]['port']
            vrf = config_facts['DEVICE_NEIGHBOR'][k]['name']
            primary = rev_vrf_map[vrf]
            new_intf = convergence_info['converged_peers'][primary]['intf_mapping'][vrf]['orig_intf_map'][exp_intf]
            assert v['chassis']['name'] == primary
            assert v['port']['ifname'] == new_intf
        else:
            # Compare the LLDP neighbor name with minigraph neigbhor name (exclude the management port)
            assert v['chassis']['name'] == config_facts['DEVICE_NEIGHBOR'][k]['name']
            assert v['chassis']['name'] == config_facts['DEVICE_NEIGHBOR'][k]['name'], (
                "LLDP neighbor name mismatch. Expected '{}', but got '{}'."
            ).format(
                config_facts['DEVICE_NEIGHBOR'][k]['name'],
                v['chassis']['name']
            )
            # Compare the LLDP neighbor interface with minigraph neigbhor interface (exclude the management port)
            if request.config.getoption("--neighbor_type") == 'eos':
                assert v['port']['ifname'] == config_facts['DEVICE_NEIGHBOR'][k]['port'], (
                    "LLDP neighbor port interface name mismatch. Expected '{}', but got '{}'."
                ).format(
                    config_facts['DEVICE_NEIGHBOR'][k]['port'],
                    v['port']['ifname']
                )
            else:
                # Dealing with KVM that advertises port description
                assert v['port']['descr'] == config_facts['DEVICE_NEIGHBOR'][k]['port'], (
                    "LLDP neighbor port description mismatch. Expected '{}', but got '{}'."
                ).format(
                    config_facts['DEVICE_NEIGHBOR'][k]['port'],
                    v['port']['descr']
                )


def _neighbor_has_lldp_entry(localhost, hostip, snmp_community, neighbor_interface):
    """Return True if the neighbor's LLDP table contains the expected interface."""
    nei_lldp_facts = localhost.lldp_facts(
        host=hostip, version='v2c', community=snmp_community)['ansible_facts']
    return neighbor_interface in nei_lldp_facts.get('ansible_lldp_facts', {})


def _parse_lldpctl_keyvalue(stdout):
    """Parse `lldpctl -f keyvalue` output into {iface: {dotted.path: value}}.

    Lines look like: lldp.Ethernet1.chassis.name=vlab-01
    Returns a mapping keyed by the neighbor's local interface name, each value
    being a dict of the remaining dotted path (e.g. 'chassis.name') -> value.
    """
    per_iface = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("lldp.") or "=" not in line:
            continue
        path, value = line.split("=", 1)
        parts = path.split(".")
        if len(parts) < 3:
            continue
        iface = parts[1]
        sub_path = ".".join(parts[2:])
        per_iface.setdefault(iface, {})[sub_path] = value
    return per_iface


def _csonic_neighbor_lldp_facts(nbrhost, dut_hostname):
    """Build an ansible_lldp_facts-equivalent dict from a cSONiC neighbor's own lldpctl.

    cSONiC (docker-sonic-vs) neighbors do not run SNMP, so the SNMP-based
    `localhost.lldp_facts()` path used for cEOS/real-SONiC-over-SSH neighbors is
    not available. Instead we query the neighbor's local `lldpctl` (via the
    CsonicHost docker-exec interface) and reshape it to match exactly what the
    SNMP collector returns: a mapping keyed by the neighbor's local interface
    ALIAS, with the remote/DUT values:
        neighbor_sys_name, neighbor_chassis_id, neighbor_sys_desc,
        neighbor_port_id, neighbor_port_desc

    The SNMP collector keys ansible_lldp_facts by the neighbor's *own* local
    interface name (from its ifTable, which on SONiC is the port alias). That is
    the same value the DUT advertises and that the test looks up as
    neighbor_interface (= the DUT-side v['port']['local']). Note this is the
    neighbor's OWN port alias, NOT the lldpctl 'port.local' field (which carries
    the *remote*/DUT port alias). We therefore resolve each neighbor-local
    interface's alias from the neighbor's CONFIG_DB.

    Only entries whose chassis name matches the DUT are included (the neighbor's
    DUT-facing link), mirroring what the DUT-vs-neighbor comparison needs.
    """
    res = nbrhost.command("lldpctl -f keyvalue", module_ignore_errors=True)
    stdout = res.get("stdout", "") if isinstance(res, dict) else ""
    per_iface = _parse_lldpctl_keyvalue(stdout)

    facts = {}
    for iface, fields in per_iface.items():
        if fields.get("chassis.name") != dut_hostname:
            continue
        # Key by the neighbor's OWN local interface alias (what SNMP ifTable would
        # report and what the DUT advertises as v['port']['local']). Resolve from
        # the neighbor's CONFIG_DB; fall back to the interface name if unset.
        alias_res = nbrhost.command(
            'sonic-db-cli CONFIG_DB hget "PORT|{}" alias'.format(iface),
            module_ignore_errors=True)
        local_alias = (alias_res.get("stdout", "").strip()
                       if isinstance(alias_res, dict) else "") or iface
        facts[local_alias] = {
            'neighbor_sys_name': fields.get("chassis.name"),
            'neighbor_chassis_id': fields.get("chassis.mac"),
            'neighbor_sys_desc': fields.get("chassis.descr"),
            # neighbor_port_id is the DUT's port alias as seen by the neighbor,
            # i.e. the lldpctl 'port.local' (remote) field.
            'neighbor_port_id': fields.get("port.local"),
            'neighbor_port_desc': fields.get("port.descr"),
        }
    return {'ansible_lldp_facts': facts}


def _csonic_neighbor_has_lldp_entry(nbrhost, dut_hostname, neighbor_interface):
    """CLI equivalent of _neighbor_has_lldp_entry for cSONiC neighbors."""
    facts = _csonic_neighbor_lldp_facts(nbrhost, dut_hostname)
    return neighbor_interface in facts.get('ansible_lldp_facts', {})


def check_lldp_neighbor(duthost, localhost, nbrhosts, eos, sonic, collect_techsupport_all_duts,
                        enum_rand_one_frontend_asic_index, tbinfo, request):
    """ verify LLDP information on neighbors """
    asic = enum_rand_one_frontend_asic_index

    res = duthost.shell(
        "docker exec -i lldp{} lldpcli show chassis | grep \"SysDescr:\" | sed -e 's/^\\s*SysDescr:\\s*//g'".format(
            '' if asic is None else asic))
    dut_system_description = res['stdout']
    internal_port_list = get_dpu_npu_ports_from_hwsku(duthost)
    lldpctl_facts = duthost.lldpctl_facts(
        asic_instance_id=asic,
        skip_interface_pattern_list=["eth0", "Ethernet-BP", "Ethernet-IB"] + internal_port_list)['ansible_facts']
    config_facts = duthost.asic_instance(asic).config_facts(host=duthost.hostname, source="running")['ansible_facts']
    if not list(lldpctl_facts['lldpctl'].items()):
        pytest.fail("No LLDP neighbors received (lldpctl_facts are empty)")
    # We use the MAC of mgmt port to generate chassis ID as LLDPD dose.
    # To be compatible with PR #3331, we keep using router MAC on T2 devices
    switch_mac = ""
    if tbinfo["topo"]["type"] != "t2":
        mgmt_alias = duthost.get_extended_minigraph_facts(tbinfo)["minigraph_mgmt_interface"]["alias"]
        switch_mac = duthost.get_dut_iface_mac(mgmt_alias)
    elif tbinfo["topo"]["type"] == "t2":
        switch_mac = config_facts['DEVICE_METADATA']['localhost']['mac'].lower()
    else:
        switch_mac = duthost.facts['router_mac']

    nei_meta = config_facts.get('DEVICE_NEIGHBOR_METADATA', {})

    for k, v in list(lldpctl_facts['lldpctl'].items()):
        try:
            hostip = v['chassis']['mgmt-ip']
        except Exception:
            logger.info("Neighbor device {} does not sent management IP via lldp".format(v['chassis']['name']))
            hostip = nei_meta[v['chassis']['name']]['mgmt_addr']

        neighbor_type = request.config.getoption("--neighbor_type")
        if neighbor_type == 'eos':
            neighbor_interface = v['port']['ifname']
            snmp_community = eos['snmp_rocommunity']
        else:
            neighbor_interface = v['port']['local']
            snmp_community = sonic['snmp_rocommunity']

        if neighbor_type == 'csonic':
            # cSONiC (docker-sonic-vs) neighbors do not run SNMP, so query the
            # neighbor's own lldpctl via the CsonicHost docker-exec interface and
            # reshape it to the same ansible_lldp_facts contract the SNMP path
            # produces. nbrhosts is keyed by neighbor name (e.g. ARISTA01T1).
            nbr_name = v['chassis']['name']
            if nbr_name not in nbrhosts:
                logger.info("Skipping LLDP neighbor verification for '{}' on '{}': "
                            "not a managed cSONiC neighbor".format(nbr_name, k))
                continue
            nbrhost = nbrhosts[nbr_name]['host']

            # After swss restart, the DUT's LLDP entry on the neighbor may have
            # aged out during the restart window. Wait until the neighbor
            # re-learns DUT's LLDP info.
            assert wait_until(30, 5, 0, _csonic_neighbor_has_lldp_entry,
                              nbrhost, duthost.hostname, neighbor_interface), \
                "Neighbor {} did not learn LLDP on interface '{}' within 30s".format(
                    nbr_name, neighbor_interface)

            nei_lldp_facts = _csonic_neighbor_lldp_facts(nbrhost, duthost.hostname)
        else:
            # After swss restart, the DUT's LLDP entry on the neighbor may have aged out
            # during the restart window. Wait until the neighbor re-learns DUT's LLDP info.
            assert wait_until(30, 5, 0, _neighbor_has_lldp_entry,
                              localhost, hostip, snmp_community, neighbor_interface), \
                "Neighbor {} did not learn LLDP on interface '{}' within 30s".format(
                    hostip, neighbor_interface)

            nei_lldp_facts = localhost.lldp_facts(
                host=hostip, version='v2c', community=snmp_community)['ansible_facts']

        # Verify the published DUT system name field is correct
        assert nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_sys_name'] == duthost.hostname, (
            "LLDP neighbor system name mismatch for interface '{}'. "
            "Expected '{}', but got '{}'."
        ).format(
            neighbor_interface,
            duthost.hostname,
            nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_sys_name']
        )

        # Verify the published DUT chassis id field is not empty
        if request.config.getoption("--neighbor_type") == 'eos':
            assert nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_chassis_id'] == \
                "0x%s" % (switch_mac.replace(':', '')), (
                "LLDP neighbor chassis ID mismatch for interface '{}'. "
                "Expected chassis ID: '{}', but got: '{}'."
            ).format(
                neighbor_interface,
                "0x%s" % (switch_mac.replace(':', '')),
                nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_chassis_id']
            )

        else:
            assert nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_chassis_id'] == switch_mac, (
                "LLDP neighbor chassis ID mismatch for interface '{}'. "
                "Expected chassis ID: '{}', but got: '{}'."
            ).format(
                neighbor_interface,
                switch_mac,
                nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_chassis_id']
            )

        # Verify the published DUT system description field is correct
            assert (
                nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_sys_desc']
                == dut_system_description
            ), (
                "LLDP neighbor system description mismatch for interface '{}'. "
                "Expected system description: '{}', but got: '{}'."
            ).format(
                neighbor_interface,
                dut_system_description,
                nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_sys_desc']
            )

        # Verify the published DUT port id field is correct
            assert nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_port_id'] == \
                config_facts['PORT'][k]['alias'], (
                "LLDP neighbor port ID mismatch for interface '{}'. "
                "Expected port ID (alias) from config_facts: '{}', but got from LLDP: '{}'."
            ).format(
                neighbor_interface,
                config_facts['PORT'][k]['alias'],
                nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_port_id']
            )

        # Verify the published DUT port description field is correct
            assert nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_port_desc'] == \
                config_facts['PORT'][k]['description'], (
                "LLDP neighbor port description mismatch for interface '{}'. "
                "Expected port description from config_facts: '{}', but got from LLDP: '{}'."
            ).format(
                neighbor_interface,
                config_facts['PORT'][k]['description'],
                nei_lldp_facts['ansible_lldp_facts'][neighbor_interface]['neighbor_port_desc']
            )


def test_lldp_neighbor(duthosts, enum_rand_one_per_hwsku_frontend_hostname, localhost, nbrhosts, eos, sonic,
                       collect_techsupport_all_duts, loganalyzer, enum_frontend_asic_index, tbinfo, request):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    if loganalyzer:
        loganalyzer[enum_rand_one_per_hwsku_frontend_hostname].ignore_regex.extend([
            ".*ERR syncd#syncd: :- check_fdb_event_notification_data.*",
            ".*ERR syncd#syncd: :- process_on_fdb_event: invalid OIDs in fdb \
                notifications, NOT translating and NOT storing in ASIC DB.*",
            ".*ERR syncd#syncd: :- process_on_fdb_event: FDB notification was \
                not sent since it contain invalid OIDs, bug.*",
        ])
    check_lldp_neighbor(duthost, localhost, nbrhosts, eos, sonic, collect_techsupport_all_duts,
                        enum_frontend_asic_index, tbinfo, request)


@pytest.mark.disable_loganalyzer
def test_lldp_neighbor_post_swss_reboot(duthosts, enum_rand_one_per_hwsku_frontend_hostname, localhost, nbrhosts, eos,
                                        sonic, collect_techsupport_all_duts, enum_frontend_asic_index,
                                        tbinfo, request, restart_swss_container):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    check_lldp_neighbor(duthost, localhost, nbrhosts, eos, sonic, collect_techsupport_all_duts,
                        enum_frontend_asic_index, tbinfo, request)


def get_expected_chassis_mac(duthost, asic, tbinfo):
    """
    Get the expected chassis MAC address based on topology and ASIC configuration.

    For T2 multi-ASIC: each ASIC's lldp container uses its own MAC
    For T2 single-ASIC: chassis-id uses router MAC (DEVICE_METADATA['localhost']['mac'])
    For non-T2: chassis-id uses management interface MAC

    Args:
        duthost: DUT host object
        asic: ASIC instance
        tbinfo: Testbed info

    Returns:
        str: Expected chassis MAC address (lowercase)
    """
    if tbinfo["topo"]["type"] == "t2":
        if duthost.is_multi_asic:
            asic_cfg = asic.config_facts(host=duthost.hostname, source="running")['ansible_facts']
            return asic_cfg['DEVICE_METADATA']['localhost']['mac'].lower()
        else:
            config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
            return config_facts['DEVICE_METADATA']['localhost']['mac'].lower()
    else:
        mgmt_alias = duthost.get_extended_minigraph_facts(tbinfo)["minigraph_mgmt_interface"]["alias"]
        return duthost.get_dut_iface_mac(mgmt_alias).lower()


def verify_lldp_table(duthost, intf_status_output, test_name=""):
    """
    Verify LLDP table interfaces match interface status (admin up, no PortChannels).

    Args:
        duthost: DUT host object
        intf_status_output: List of interface status dictionaries
        test_name: Optional test context name for logging

    Returns:
        set: LLDP table interfaces (including eth0 if present)
    """
    context = " {}".format(test_name) if test_name else ""
    logger.info("Verifying LLDP table{}".format(context))

    # Get LLDP table output using show_and_parse for robust parsing
    lldp_table_parsed = duthost.show_and_parse("show lldp table")
    lldp_table_interfaces = set()
    for entry in lldp_table_parsed:
        interface = entry.get('localport', '')
        # Filter out separator/footer lines that show_and_parse may include
        if interface and not interface.startswith('-') and not interface.startswith('Total'):
            lldp_table_interfaces.add(interface)

    logger.info("LLDP table interfaces{}: {}".format(context, sorted(lldp_table_interfaces)))
    logger.info("LLDP table interfaces in total: {}".format(len(lldp_table_interfaces)))

    # On virtual/KVM testbeds, eth0 has no LLDP neighbor so it won't appear in the LLDP table
    if is_virtual_platform(duthost):
        if 'eth0' not in lldp_table_interfaces:
            logger.info("eth0 not in LLDP table (expected on virtual/KVM testbed){}"
                        .format(context))
    else:
        pytest_assert('eth0' in lldp_table_interfaces,
                      "eth0 is missing from LLDP table{}".format(context))

    # For LLDP table comparison: exclude eth0 from lldp_table, exclude PortChannels and admin down from intf_status
    lldp_table_interfaces_no_eth0 = lldp_table_interfaces - {'eth0'}

    # Filter intf_status_output: exclude PortChannel interfaces and admin down interfaces
    intf_status_filtered_for_lldp = {
        intf['interface'] for intf in intf_status_output
        if not intf['interface'].startswith('PortChannel') and intf['admin'].lower() == 'up'
    }

    missing_in_lldp_table = intf_status_filtered_for_lldp - lldp_table_interfaces_no_eth0
    extra_in_lldp_table = lldp_table_interfaces_no_eth0 - intf_status_filtered_for_lldp

    if missing_in_lldp_table:
        logger.warning("Interfaces (admin up, no PortChannels) missing in LLDP table{}: {}".format(
            context, sorted(missing_in_lldp_table)))
    if extra_in_lldp_table:
        logger.warning("Interfaces in LLDP table but not in filtered interface status{}: {}".format(
            context, sorted(extra_in_lldp_table)))

    if not missing_in_lldp_table and not extra_in_lldp_table:
        logger.info("LLDP table and interface status (admin up, no PortChannels) match perfectly{}".format(context))

    # Only assert that LLDP table has no unexpected interfaces (extra).
    # Missing interfaces in LLDP table are expected on dualtor/some topologies
    # where admin-up ports may not have LLDP neighbors.
    if missing_in_lldp_table:
        logger.info("Interfaces admin-up but missing LLDP neighbors (expected on some topologies){}: {}".format(
            context, sorted(missing_in_lldp_table)))
    pytest_assert(not extra_in_lldp_table,
                  "Extra interfaces in LLDP table (not in admin-up non-PortChannel set){}: {}".format(
                      context, sorted(extra_in_lldp_table)))

    return lldp_table_interfaces


def verify_lldpcli_interfaces(duthost, asic, intf_status_output, test_name=""):
    """
    Verify lldpcli interfaces match interface status (all interfaces, no PortChannels).

    Args:
        duthost: DUT host object
        asic: ASIC instance
        intf_status_output: List of interface status dictionaries
        test_name: Optional test context name for logging

    Returns:
        set: lldpcli interfaces (including eth0 if present)
    """
    context = " {}".format(test_name) if test_name else ""
    logger.info("Verifying lldpcli show interfaces{}".format(context))

    # Get lldpcli interfaces
    lldpcli_output = duthost.shell(
        "docker exec lldp{} lldpcli show interfaces".format(
            asic.asic_index if duthost.is_multi_asic else ""
        )
    )['stdout']

    lldpcli_interfaces = set()
    for line in lldpcli_output.split('\n'):
        if line.startswith('Interface:'):
            interface = line.split('Interface:')[1].strip()
            lldpcli_interfaces.add(interface)

    logger.info("lldpcli interfaces{}: {}".format(context, sorted(lldpcli_interfaces)))
    logger.info("lldpcli interfaces in total: {}".format(len(lldpcli_interfaces)))

    # On virtual/KVM testbeds, eth0 may not appear in lldpcli
    if is_virtual_platform(duthost):
        if 'eth0' not in lldpcli_interfaces:
            logger.info("eth0 not in lldpcli interfaces (expected on virtual/KVM testbed){}"
                        .format(context))
    else:
        pytest_assert('eth0' in lldpcli_interfaces,
                      "eth0 is missing from lldpcli interfaces{}".format(context))

    # For lldpcli comparison: exclude eth0 from lldpcli, exclude only PortChannels from intf_status
    lldpcli_interfaces_no_eth0 = lldpcli_interfaces - {'eth0'}

    # Filter intf_status_output: exclude only PortChannel interfaces (keep admin down)
    # On multi-ASIC, lldpcli only shows interfaces for the current ASIC,
    # so also filter intf_status to only include ports belonging to this ASIC.
    if duthost.is_multi_asic:
        asic_cfg = asic.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        asic_ports = set(asic_cfg.get('PORT', {}).keys())
        logger.info("Multi-ASIC: filtering interface status to ASIC {} ports ({} ports)".format(
            asic.asic_index, len(asic_ports)))
        intf_status_filtered_for_lldpcli = {
            intf['interface'] for intf in intf_status_output
            if not intf['interface'].startswith('PortChannel') and intf['interface'] in asic_ports
        }
    else:
        intf_status_filtered_for_lldpcli = {
            intf['interface'] for intf in intf_status_output
            if not intf['interface'].startswith('PortChannel')
        }

    missing_in_lldpcli = intf_status_filtered_for_lldpcli - lldpcli_interfaces_no_eth0
    extra_in_lldpcli = lldpcli_interfaces_no_eth0 - intf_status_filtered_for_lldpcli

    if missing_in_lldpcli:
        logger.warning("Interfaces (no PortChannels) missing in lldpcli{}: {}".format(
            context, sorted(missing_in_lldpcli)))
    if extra_in_lldpcli:
        logger.warning("Interfaces in lldpcli but not in interface status{}: {}".format(
            context, sorted(extra_in_lldpcli)))

    if not missing_in_lldpcli and not extra_in_lldpcli:
        logger.info("lldpcli and interface status (no PortChannels) match perfectly{}".format(context))

    pytest_assert(intf_status_filtered_for_lldpcli == lldpcli_interfaces_no_eth0,
                  "Interface mismatch between 'show interface status' (no PortChannels) and lldpcli{}. "
                  "Missing in lldpcli: {}, Extra in lldpcli: {}".format(
                      context, sorted(missing_in_lldpcli), sorted(extra_in_lldpcli)))

    return lldpcli_interfaces


def verify_lldpctl_facts(duthost, enum_frontend_asic_index, intf_status_output, lldpcli_interfaces, test_name=""):
    """
    Verify lldpctl_facts interfaces match interface status (admin up, no PortChannels).

    Args:
        duthost: DUT host object
        enum_frontend_asic_index: Frontend ASIC index
        intf_status_output: List of interface status dictionaries
        lldpcli_interfaces: Set of lldpcli interfaces for consistency check
        test_name: Optional test context name for logging

    Returns:
        dict: lldpctl_facts ansible_facts
    """
    context = " {}".format(test_name) if test_name else ""
    logger.info("Verifying lldpctl_facts{}".format(context))

    # Get lldpctl_facts
    internal_port_list = get_dpu_npu_ports_from_hwsku(duthost)
    lldpctl_facts = duthost.lldpctl_facts(
        asic_instance_id=enum_frontend_asic_index,
        skip_interface_pattern_list=["Ethernet-BP", "Ethernet-IB"] + internal_port_list
    )['ansible_facts']

    # Verify eth0 is in lldpctl_facts (only on physical testbeds)
    if is_virtual_platform(duthost):
        if 'eth0' not in lldpctl_facts.get('lldpctl', {}):
            logger.info("eth0 not in lldpctl_facts (expected on virtual/KVM testbed){}"
                        .format(context))
    else:
        pytest_assert('eth0' in lldpctl_facts.get('lldpctl', {}),
                      "eth0 is missing from lldpctl_facts{}".format(context))

    # Get interfaces from lldpctl_facts (excluding eth0)
    lldpctl_facts_interfaces = set(lldpctl_facts.get('lldpctl', {}).keys()) - {'eth0'}
    logger.info("lldpctl_facts interfaces (excluding eth0){}: {}".format(
        context, sorted(lldpctl_facts_interfaces)))
    logger.info("lldpctl_facts interfaces in total: {}".format(len(lldpctl_facts_interfaces)))

    # Compare intf_status_output with lldpctl_facts interfaces (exclude PortChannels and admin down from intf_status)
    intf_status_filtered_for_lldpctl = {
        intf['interface'] for intf in intf_status_output
        if not intf['interface'].startswith('PortChannel') and intf['admin'].lower() == 'up'
    }

    missing_in_lldpctl_facts = intf_status_filtered_for_lldpctl - lldpctl_facts_interfaces
    extra_in_lldpctl_facts = lldpctl_facts_interfaces - intf_status_filtered_for_lldpctl

    if missing_in_lldpctl_facts:
        logger.warning("Interfaces in 'show interface status' but missing in lldpctl_facts{}: {}".format(
            context, sorted(missing_in_lldpctl_facts)))
    if extra_in_lldpctl_facts:
        logger.warning("Interfaces in lldpctl_facts but not in 'show interface status'{}: {}".format(
            context, sorted(extra_in_lldpctl_facts)))

    if not missing_in_lldpctl_facts and not extra_in_lldpctl_facts:
        logger.info("lldpctl_facts and interface status (admin up, no PortChannels) match perfectly{}".format(context))

    # Only assert that lldpctl_facts has no unexpected interfaces.
    # Missing interfaces are expected on dualtor/some topologies where
    # admin-up ports may not have LLDP neighbors.
    if missing_in_lldpctl_facts:
        logger.info("Interfaces admin-up but missing in lldpctl_facts (expected on some topologies){}: {}".format(
            context, sorted(missing_in_lldpctl_facts)))
    pytest_assert(not extra_in_lldpctl_facts,
                  "Unexpected interfaces in lldpctl_facts (not admin-up or are PortChannels){}: {}".format(
                      context, sorted(extra_in_lldpctl_facts)))

    # Verify consistency between lldpctl_facts and lldpcli
    for interface in lldpctl_facts.get('lldpctl', {}):
        pytest_assert(interface in lldpcli_interfaces,
                      "Interface {} from lldpctl_facts is missing in lldpcli interfaces{}".format(
                          interface, context))

    return lldpctl_facts


def verify_chassis_info(duthost, asic, expected_chassis_mac, test_name=""):
    """
    Verify chassis ID and capabilities.

    Args:
        duthost: DUT host object
        asic: ASIC instance
        expected_chassis_mac: Expected chassis MAC address
        test_name: Optional test context name for logging
    """
    context = " {}".format(test_name) if test_name else ""
    logger.info("Verifying Chassis ID and Capabilities{}".format(context))

    # Get chassis information
    chassis_output = duthost.shell(
        "docker exec lldp{} lldpcli show chassis".format(
            asic.asic_index if duthost.is_multi_asic else ""
        )
    )['stdout']

    logger.info("Chassis output{}:\n{}".format(context, chassis_output))

    # Verify ChassisID type is mac
    chassis_id_match = re.search(r'ChassisID:\s+mac\s+([0-9a-f:]+)', chassis_output, re.IGNORECASE)
    pytest_assert(chassis_id_match is not None,
                  "ChassisID with type 'mac' not found in chassis output{}".format(context))

    actual_chassis_mac = chassis_id_match.group(1).lower()
    pytest_assert(actual_chassis_mac == expected_chassis_mac,
                  "Chassis MAC mismatch{}. Expected: {}, Got: {}".format(
                      context, expected_chassis_mac, actual_chassis_mac))

    # Verify Capabilities are present with correct status
    pytest_assert(re.search(r'Capability:\s+Bridge,\s+on', chassis_output, re.IGNORECASE),
                  "Bridge capability should be 'on' in chassis output{}".format(context))
    pytest_assert(re.search(r'Capability:\s+Router,\s+on', chassis_output, re.IGNORECASE),
                  "Router capability should be 'on' in chassis output{}".format(context))
    # Wlan and Station capabilities: verify off if present (not all platforms report them)
    wlan_match = re.search(r'Capability:\s+Wlan,\s+(\w+)', chassis_output, re.IGNORECASE)
    if wlan_match:
        pytest_assert(wlan_match.group(1).lower() == 'off',
                      "Wlan capability should be 'off' in chassis output{}".format(context))
    station_match = re.search(r'Capability:\s+Station,\s+(\w+)', chassis_output, re.IGNORECASE)
    if station_match:
        pytest_assert(station_match.group(1).lower() == 'off',
                      "Station capability should be 'off' in chassis output{}".format(context))


def test_lldp_interfaces(duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                         enum_frontend_asic_index, tbinfo, loganalyzer):
    """
    Test LLDP functionality to verify all interfaces and chassis information are correct.
    This test is similar to test_lldp_interface_config_reload but without performing config reload.

    Steps:
    1. Record all interfaces from 'show interface status'
    2. Verify LLDP table matches recorded interfaces
    3. Verify lldpcli interfaces match recorded interfaces
    4. Verify lldpctl_facts interfaces match recorded interfaces
    5. Verify chassis ID and capabilities
    6. Check syslog for LLDP errors using loganalyzer
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asic = duthost.asic_instance(enum_frontend_asic_index)

    # Configure loganalyzer to only fail on LLDP-specific errors
    if loganalyzer:
        # Override match_regex for ALL DUTs to only match LLDP errors
        # (the fixture teardown analyzes all DUTs, not just the selected one)
        for hostname in loganalyzer:
            loganalyzer[hostname].match_regex = [
                ".*cannot find port.*",
                ".*ERR lldp#lldpmgrd.*"
            ]

    with loganalyzer[enum_rand_one_per_hwsku_frontend_hostname] if loganalyzer else contextlib.nullcontext():
        logger.info("Step 1: Recording all interfaces")
        # Get all interfaces from 'show interface status' using show_and_parse
        intf_status_output = duthost.show_and_parse("show interface status")

        # Save all original interfaces
        all_interfaces = {intf['interface'] for intf in intf_status_output}
        logger.info("All interfaces from 'show interface status': {}".format(sorted(all_interfaces)))
        logger.info("All interfaces in total: {}".format(len(all_interfaces)))

        # Get expected chassis MAC address
        expected_chassis_mac = get_expected_chassis_mac(duthost, asic, tbinfo)
        logger.info("Expected chassis MAC address: {}".format(expected_chassis_mac))

        # Step 2: Verify LLDP table
        verify_lldp_table(duthost, intf_status_output)

        # Step 3: Verify lldpcli interfaces
        lldpcli_interfaces = verify_lldpcli_interfaces(duthost, asic, intf_status_output)

        # Step 4: Verify lldpctl_facts
        verify_lldpctl_facts(duthost, enum_frontend_asic_index, intf_status_output, lldpcli_interfaces)

        # Step 5: Verify chassis ID and capabilities
        verify_chassis_info(duthost, asic, expected_chassis_mac)

    logger.info("Test completed successfully. All LLDP checks passed.")


def test_lldp_interfaces_config_reload(duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                                       enum_frontend_asic_index, tbinfo, loganalyzer):
    """
    Test LLDP functionality after config reload to verify all interfaces and chassis information are correct.
    This test covers the issue: https://github.com/sonic-net/sonic-mgmt/issues/22376

    Steps:
    1. Record all interfaces before the test
    2. Perform config reload
    3. Verify LLDP table matches recorded interfaces
    4. Verify lldpcli interfaces match recorded interfaces
    5. Verify lldpctl_facts interfaces match recorded interfaces
    6. Verify chassis ID and capabilities
    7. Check syslog for LLDP errors using loganalyzer
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asic = duthost.asic_instance(enum_frontend_asic_index)

    # Configure loganalyzer to only fail on LLDP-specific errors
    if loganalyzer:
        # Override match_regex for ALL DUTs to only match LLDP errors
        # (the fixture teardown analyzes all DUTs, not just the selected one)
        for hostname in loganalyzer:
            loganalyzer[hostname].match_regex = [
                ".*cannot find port.*",
                ".*ERR lldp#lldpmgrd.*"
            ]

    with loganalyzer[enum_rand_one_per_hwsku_frontend_hostname] if loganalyzer else contextlib.nullcontext():
        logger.info("Step 1: Recording all interfaces before config reload")
        # Get all interfaces from 'show interface status' using show_and_parse
        intf_status_output = duthost.show_and_parse("show interface status")

        # Save all original interfaces
        all_pre_reload_interfaces = {intf['interface'] for intf in intf_status_output}
        logger.info("All interfaces before config reload: {}".format(sorted(all_pre_reload_interfaces)))
        logger.info("All interfaces in total: {}".format(len(all_pre_reload_interfaces)))

        # Get expected chassis MAC address before reload
        expected_chassis_mac = get_expected_chassis_mac(duthost, asic, tbinfo)
        logger.info("Expected chassis MAC address: {}".format(expected_chassis_mac))

        # Record pre-reload LLDP neighbor count as baseline
        # (not all admin-up ports have neighbors, e.g. dualtor/unused ports)
        pre_reload_lldp_neighbors = get_num_lldpctl_facts(duthost, enum_frontend_asic_index)
        logger.info("Pre-reload LLDP neighbor count: {}".format(pre_reload_lldp_neighbors))
        pytest_assert(pre_reload_lldp_neighbors > 0,
                      "No LLDP neighbors found before config reload")

        logger.info("Step 2: Performing config reload")
        config_reload(duthost, safe_reload=True, check_intf_up_ports=True, wait_for_bgp=True)

        logger.info("Step 3: Waiting for system to stabilize after config reload")
        # Wait for LLDP to converge
        assert wait_until(300, 10, 0, duthost.critical_services_fully_started), \
            "Not all critical services are fully started after config reload"

        # Wait for all LLDP neighbors to be re-discovered using pre-reload count as baseline
        pytest_assert(
            wait_until(180, 10, 0, lambda: get_num_lldpctl_facts(
                duthost, enum_frontend_asic_index) >= pre_reload_lldp_neighbors),
            "Expected {} LLDP neighbors but only found {} after config reload".format(
                pre_reload_lldp_neighbors, get_num_lldpctl_facts(duthost, enum_frontend_asic_index))
        )

        # Step 4: Verify LLDP table after config reload
        verify_lldp_table(duthost, intf_status_output, "after config reload")

        # Step 5: Verify lldpcli interfaces after config reload
        lldpcli_interfaces = verify_lldpcli_interfaces(duthost, asic, intf_status_output, "after config reload")

        # Step 6: Verify lldpctl_facts after config reload
        verify_lldpctl_facts(duthost, enum_frontend_asic_index, intf_status_output,
                             lldpcli_interfaces, "after config reload")

        # Step 7: Verify chassis ID and capabilities after config reload
        verify_chassis_info(duthost, asic, expected_chassis_mac, "after config reload")

    logger.info("Test completed successfully. All LLDP checks passed after config reload.")
