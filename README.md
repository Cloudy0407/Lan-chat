# LAN Chat & File Sharing (P2P)

A peer-to-peer (P2P) CLI chat and file-sharing application over LAN using UDP broadcast for peer discovery and TCP for framed messaging and concurrent file transfer.

## 1. Installation

### Prerequisites
- Python **3.10** or higher.
- Devices must be on the same LAN/Wi-Fi network with UDP port `9999` (default) and your chosen TCP ports allowed through the OS firewall.

### Setup Steps
Open a terminal inside the extracted source folder and set up a virtual environment:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (CMD):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the required runtime package (`prompt_toolkit`):
```bash
pip install prompt_toolkit
```

---

## 2. How to Run

### Across Multiple Machines on the Same LAN / Wi-Fi
Run the program on each machine with a unique `--name` and a TCP `--port`:

```bash
# Machine 1
python src/main.py --name Alice --port 5001

# Machine 2
python src/main.py --name Bob --port 5002
```

Wait ~4 seconds for the duplicate-name check to complete. Online peers will appear automatically once UDP discovery packets are received.

### On a Single Machine (2 Terminals)
Open two terminals with the virtual environment activated and assign **different TCP ports**:

**Windows:**
```powershell
# Terminal 1
python src\main.py --name Duy --port 5001

# Terminal 2
python src\main.py --name Khang --port 5002
```
> **Note:** If peers do not discover each other with the default `255.255.255.255`, run `ipconfig` to check your Wi-Fi IPv4 subnet (e.g., `192.168.1.x`) and pass `--bcast 192.168.1.255`. Make sure Python is allowed in Windows Defender Firewall (Private networks).

**Linux / macOS (works offline via loopback broadcast):**
```bash
# Terminal 1
python3 src/main.py --name Duy --port 5001 --bcast 127.255.255.255

# Terminal 2
python3 src/main.py --name Khang --port 5002 --bcast 127.255.255.255
```

### Command-Line Arguments

| Option | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `--name` | Yes | — | Display name (1–32 UTF-8 bytes, no `\|`, newlines, or double spaces) |
| `--port` | No | `5000` | TCP listening port (`1–65535`, or `0` for an OS-assigned port) |
| `--udp-port` | No | `9999` | UDP discovery port (`1–65535`) |
| `--bcast` | No | `255.255.255.255` | UDP broadcast address |
| `--downloads` | No | `downloads` | Folder for saving received files |

### Interactive Commands

| Command | Description |
| :--- | :--- |
| `/list` | Show online peers (numbered, with IP:port, status, and `*` for current partner) |
| `/chat <name\|number>` | Open or switch chat with a peer by name or list number |
| `<any text>` | Send a UTF-8 message to the current chat partner |
| `/send <file_path>` | Send a file to the current partner in the background |
| `/leave` | Leave the current chat session (`BYE\|`) |
| `/status <online\|away>` | Change presence status and broadcast immediately |
| `/help` | Show all available commands |
| `/quit` | Disconnect cleanly from all peers and exit |

---

## 3. How to Test

With two instances running (`Duy` on port `5001` and `Khang` on port `5002`):

1. **Discovery & Duplicate Name:** Wait ~4–7 seconds until both terminals show `<peer> is online`. Launch a third instance using `--name Duy --port 5003` and verify it rejects the duplicate name and exits.
2. **Presence & Timeout:** Run `/status away` on one terminal and check `/list` on the other (status updates immediately). Force-close one terminal and verify the remaining peer reports it offline after ~10 seconds.
3. **Chat & Unread Messages:** Run `/list` then `/chat 1` (or `/chat Khang`), exchange UTF-8/Vietnamese messages, and verify messages from a non-active peer accumulate as unread until you switch to them.
4. **Concurrent File Transfer:** Run `/send <path_to_large_file>` while sending chat messages at the same time. Verify the bottom toolbar shows live transfer progress, chat stays responsive, the file is saved in `downloads/`, and checksums match (`Get-FileHash <file>` on PowerShell or `sha256sum <file>` on Linux/macOS).
5. **Graceful Disconnect:** Run `/leave` and `/quit` and confirm the remote peer displays `[*] <name> left the chat.` without error.
