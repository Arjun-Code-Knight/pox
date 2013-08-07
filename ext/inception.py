"""
Inception Cloud SDN controller
"""

from pox.core import core
from pox.lib.util import dpid_to_str
from pox import log
from pox.log import color
import pox.openflow.libopenflow_01 as of
from ext.inception_arp import InceptionArp
from ext.inception_dhcp import InceptionDhcp

LOGGER = core.getLogger()

IP_PREFIX = "10.2"

FWD_PRIORITY = 15


class Inception(object):
    """
    Inception cloud SDN controller
    """

    def __init__(self):
        core.openflow.addListeners(self)
        ## data stuctures
        # MAC => (dpid, port): mapping from MAC address to (switch
        # dpid, switch port) of end hosts
        self.mac_to_dpid_port = {}
        # dpid -> IP address: records the mapping from switch dpid) to
        # IP address of the rVM where it resides. This table is to
        # facilitate the look-up of dpid_ip_to_port
        self.dpid_to_ip = {}
        # (dpid, IP address) -> port: records the neighboring
        # relationship between switches. It is mapping from data path
        # ID (dpid) of a switch and IP address of neighboring rVM to
        # port number. Its semantics is that each entry stands for
        # connection between switches via some specific port. VXLan,
        # however, only stores information of IP address of rVM in
        # which neighbor switches lies.  Rather than storing the
        # mapping from dpid to dpid directly, we store mapping from
        # dpid to IP address. With further look-up in dpid_to_ip, the
        # dpid to dpid mapping can be retrieved.
        self.dpid_ip_to_port = {}
        ## modules
        # ARP
        self.inception_arp = InceptionArp(self)
        # DHCP
        self.inception_dhcp = InceptionDhcp(self)

    def _handle_ConnectionUp(self, event):
        """
        Handle when a switch is connected
        """
        switch_id = event.dpid
        switch_features = event.ofp
        connection = event.connection
        sock = connection.sock
        ip, port = sock.getpeername()

        # If the entry corresponding to the MAC already exists
        if switch_id in self.dpid_to_ip:
            LOGGER.info("switch=%s already connected", dpid_to_str(switch_id))
        else:
            self.dpid_to_ip[switch_id] = ip
            LOGGER.info("Add: switch=%s -> ip=%s", dpid_to_str(switch_id), ip)

        # Collect port information.  Sift out ports connecting peer
        # switches and store them in dpid_ip_to_port
        for port in switch_features.ports:
            # FIXME(changbl): Parse the port name to get the IP
            # address of remote rVM to which the bridge builds a
            # VXLAN. E.g., obr1_184-53 => IP_PREFIX.184.53. Only store
            # the port connecting remote rVM.
            if port.name.startswith('obr') and '_' in port.name:
                _, ip_suffix = port.name.split('_')
                ip_suffix = ip_suffix.replace('-', '.')
                peer_ip = '.'.join((IP_PREFIX, ip_suffix))
                self.dpid_ip_to_port[(switch_id, peer_ip)] = port.port_no
                LOGGER.info("Add: (switch=%s, peer_ip=%s) -> port=%s",
                            dpid_to_str(switch_id), peer_ip, port.port_no)

    def _handle_ConnectionDown(self, event):
        """
        Handle when a switch turns off connection
        """
        switch_id = event.dpid
        # Delete switch's mapping from MAC address to remote IP address
        LOGGER.info("Del: switch=%s -> ip=%s", dpid_to_str(switch_id),
                    self.dpid_to_ip[switch_id])
        del self.dpid_to_ip[switch_id]
        # Delete all its port information
        for key in self.dpid_ip_to_port.keys():
            (dpid, ip) = key
            if switch_id == dpid:
                LOGGER.info("Del: (switch=%s, peer_ip=%s) -> port=%s",
                            dpid_to_str(dpid), ip, self.dpid_ip_to_port[key])
                del self.dpid_ip_to_port[key]

    def _handle_PacketIn(self, event):
        """
        Handle when a packet is received
        """
        # If packet is not parsed properly, alert and return
        eth_packet = event.parsed
        if not eth_packet.parsed:
            LOGGER.warning("Unparsable packet")
            return

        # do source learning
        self._do_source_learning(event)
        # handle ARP packet if it is
        self.inception_arp.handle(event)
        # handle DHCP packet if it is
        self.inception_dhcp.handle(event)

    def _do_source_learning(self, event):
        """
        Learn MAC => (switch dpid, switch port) mapping from a packet,
        update self.mac_to_dpid_port table
        """
        eth_packet = event.parsed
        if eth_packet.src not in self.mac_to_dpid_port:
            self.mac_to_dpid_port[eth_packet.src] = (event.dpid, event.port)
            LOGGER.info("Learn: host=%s -> (switch=%s, port=%s)",
                        eth_packet.src, dpid_to_str(event.dpid), event.port)

    def setup_fwd_flows(self, dst_mac, switch_id, fwd_port,
                        peer_switch_id, peer_fwd_port):
        """
        Setup two flows for data forwarding, one on a switch, one on its peer
        """
        # The first flow at switch
        core.openflow.sendToDPID(switch_id, of.ofp_flow_mod(
            match=of.ofp_match(dl_dst=dst_mac),
            action=of.ofp_action_output(port=fwd_port),
            priority=FWD_PRIORITY))
        LOGGER.info("Setup forward flow on switch=%s for dst_mac=%s",
                    dpid_to_str(switch_id), dst_mac)
        # The second flow at its peer switch
        core.openflow.sendToDPID(peer_switch_id, of.ofp_flow_mod(
            match=of.ofp_match(dl_dst=dst_mac),
            action=of.ofp_action_output(port=peer_fwd_port),
            priority=FWD_PRIORITY))
        LOGGER.info("Setup forward flow on switch=%s for dst_mac=%s",
                    dpid_to_str(peer_switch_id), dst_mac)


def launch():
    """ Register the component to core"""
    color.launch()
    log.launch(format="%(asctime)s - %(name)s - %(levelname)s - "
               "%(threadName)s - %(message)s")
    core.registerNew(Inception)
    LOGGER.info("InceptionArp is started...")