import logging
import ipaddress

from tests.common.utilities import wait_until
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.common.devices.eos import EosHost
try:
    from tests.common.devices.csonic import CsonicHost
except ImportError:  # pragma: no cover - csonic module optional in some trees
    CsonicHost = None

logger = logging.getLogger(__name__)

DEF_WAIT_TIMEOUT = 300
DEF_CHECK_INTERVAL = 10
SNMP_DEFAULT_TIMEOUT = 20

global_snmp_facts = {}


def is_snmp_subagent_running(duthost):
    cmd = "docker exec snmp supervisorctl status snmp-subagent"
    output = duthost.shell(cmd)
    if "RUNNING" in output["stdout"]:
        logger.info("SNMP Sub-Agent is Running")
        return True
    logger.info("SNMP Sub-Agent is Not Running")
    return False


def _get_snmp_facts(localhost, host, version, community, is_dell, include_swap, module_ignore_errors,
                    timeout=SNMP_DEFAULT_TIMEOUT):
    snmp_facts = localhost.snmp_facts(host=host, version=version, community=community, is_dell=is_dell,
                                      module_ignore_errors=module_ignore_errors, include_swap=include_swap,
                                      timeout=timeout)
    return snmp_facts


def _update_snmp_facts(localhost, host, version, community, is_dell, include_swap, duthost,
                       timeout=SNMP_DEFAULT_TIMEOUT):
    global global_snmp_facts

    try:
        snmp_subagent_running = is_snmp_subagent_running(duthost)
        global_snmp_facts = _get_snmp_facts(localhost, host, version, community, is_dell, include_swap,
                                            module_ignore_errors=False, timeout=timeout)
    except RunAnsibleModuleFail as e:
        logger.info("encountered error when getting snmp facts: {}".format(e))
        global_snmp_facts = {}
        return False

    return snmp_subagent_running and True


def get_snmp_facts(duthost, localhost, host, version, community, is_dell=False, module_ignore_errors=False,
                   wait=False, include_swap=False, timeout=DEF_WAIT_TIMEOUT, interval=DEF_CHECK_INTERVAL,
                   snmp_timeout=SNMP_DEFAULT_TIMEOUT):
    if not wait:
        return _get_snmp_facts(localhost, host, version, community, is_dell, include_swap, module_ignore_errors,
                               timeout=snmp_timeout)

    global global_snmp_facts

    pytest_assert(wait_until(timeout, interval, 0, _update_snmp_facts, localhost, host, version,
                             community, is_dell, include_swap, duthost, snmp_timeout), "Timeout waiting for SNMP facts")
    return global_snmp_facts


def get_snmp_output(ip, duthost, nbr, creds_all_duts, oid='.1.3.6.1.2.1.1.1.0'):
    """
    Get snmp output from duthost using specific ip to query
    snmp query is sent from neighboring ceos/vsonic

     Args:
        ip(str): IP of dut to be used to send SNMP query
        duthost: duthost
        nbr: from where the snmp query should be executed
        creds_all_duts: creds to get snmp_rocommunity of duthost
        oid: to query

    Returns:
        SNMP result
    """
    ipaddr = ipaddress.ip_address(ip)
    iptables_cmd = "iptables"

    if isinstance(ipaddr, ipaddress.IPv6Address):
        iptables_cmd = "ip6tables"

    ip_tbl_rule_add = "sudo {} -I INPUT 1 -p udp --dport 161 -d {} -j ACCEPT".format(
        iptables_cmd, ip)
    duthost.shell(ip_tbl_rule_add)
    # DUT IP is only accessible through VRF from neighboring devices if the neighbor is a multi-VRF peer
    # This enhancement only handles the case where the neighbor is EoS
    vrf_prefix = ""
    if nbr.get("is_multi_vrf_peer", False):
        vrf = nbr.get("multi_vrf_data", {}).get("vrf", "")
        if vrf:
            vrf_prefix = "sudo ip netns exec ns-{}".format(vrf)
    if isinstance(nbr["host"], EosHost):
        eos_snmpget = "bash {} snmpget -v2c -c {} {} {}".format(
            vrf_prefix, creds_all_duts[duthost.hostname]['snmp_rocommunity'], ip, oid)
        out = nbr['host'].eos_command(commands=[eos_snmpget])
    elif CsonicHost is not None and isinstance(nbr["host"], CsonicHost):
        # cSONiC (docker-sonic-vs) neighbor: CsonicHost already runs commands
        # inside the neighbor container, so run net-snmp's snmpwalk directly
        # there. The legacy "docker exec snmp ..." wrapper assumes a nested snmp
        # container and fails with rc=127 'docker: command not found' (cEOS
        # avoids this via the EosHost branch above).
        community = creds_all_duts[duthost.hostname]['snmp_rocommunity']
        # The neighbor Loopback is not routable back from the DUT, so bind the
        # query to the DUT-facing PortChannel1 global address via --clientaddr.
        if isinstance(ipaddr, ipaddress.IPv6Address):
            addr_family, iface_grep = "-6", "inet6 fc"
        else:
            addr_family, iface_grep = "-4", "inet 10."
        src_lookup = (
            "ip {af} -o addr show PortChannel1 2>/dev/null | "
            "grep -m1 '{grep}' | awk '{{print $4}}' | cut -d/ -f1"
        ).format(af=addr_family, grep=iface_grep)
        src_out = nbr['host'].command(src_lookup, module_ignore_errors=True)
        client_addr = ""
        src_stdout = src_out.get('stdout', '') if isinstance(src_out, dict) else ""
        if src_stdout and src_stdout.strip():
            client_addr = "--clientaddr={} ".format(src_stdout.strip())
        # On a docker-sonic-vs neighbor the BGP route to the DUT loopback is
        # programmed to the ASIC/FIB but is NOT present in the neighbor's Linux
        # kernel routing table, so control-plane traffic (snmpget from the host
        # netns) falls through to the default route and egresses the wrong
        # (backplane) interface -- the query never reaches the DUT. --clientaddr
        # only fixes the source address, not egress selection. Install a
        # temporary kernel /host route for the DUT IP via the BGP nexthop out
        # PortChannel1 for the duration of the query, then remove it.
        nh_family = "-6" if isinstance(ipaddr, ipaddress.IPv6Address) else "-4"
        nexthop_lookup = (
            "vtysh -c 'show {af} route {dst} json' 2>/dev/null | "
            "python3 -c \"import sys,json;"
            "d=json.load(sys.stdin);"
            "e=next(iter(d.values()))[0];"
            "n=[h for h in e['nexthops'] "
            "if h.get('interfaceName','').startswith('PortChannel')][0];"
            "print(n['ip'], n['interfaceName'])\""
        ).format(af="ipv6" if isinstance(ipaddr, ipaddress.IPv6Address) else "ip",
                 dst=ip)
        nh_out = nbr['host'].command(nexthop_lookup, module_ignore_errors=True)
        nh_stdout = nh_out.get('stdout', '') if isinstance(nh_out, dict) else ""
        route_added = False
        if nh_stdout and len(nh_stdout.split()) == 2:
            nh_ip, nh_dev = nh_stdout.split()
            add_cmd = "ip {af} route replace {dst}/{plen} via {nh} dev {dev}".format(
                af=nh_family, dst=ip,
                plen=128 if isinstance(ipaddr, ipaddress.IPv6Address) else 32,
                nh=nh_ip, dev=nh_dev)
            nbr['host'].command(add_cmd, module_ignore_errors=True)
            route_added = True
        try:
            command = "snmpwalk -v 2c -c {} {}{} {}".format(
                community, client_addr, ip, oid)
            out = nbr['host'].command(command)
        finally:
            if route_added:
                del_cmd = "ip {af} route del {dst}/{plen} via {nh} dev {dev}".format(
                    af=nh_family, dst=ip,
                    plen=128 if isinstance(ipaddr, ipaddress.IPv6Address) else 32,
                    nh=nh_ip, dev=nh_dev)
                nbr['host'].command(del_cmd, module_ignore_errors=True)
    else:
        command = "docker exec snmp snmpwalk -v 2c -c {} {} {}".format(
                  creds_all_duts[duthost.hostname]['snmp_rocommunity'], ip, oid)
        out = nbr['host'].command(command)

    ip_tbl_rule_del = "sudo {} -D INPUT -p udp --dport 161 -d {} -j ACCEPT".format(
        iptables_cmd, ip)
    duthost.shell(ip_tbl_rule_del)

    return out
