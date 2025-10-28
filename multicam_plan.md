# **Project: Pi Multi-Camera Sync-Recording System**

This document is the master plan for building a system for synchronized, remotely-triggered video recording using multiple Raspberry Pis, all managed by a central "Control Pi" running a web application.

## **1\. System Architecture**

* **1x Control Pi (Master):** A Raspberry Pi (Pi 4 or 5 recommended) that acts as the "brain".  
  * Runs the local **NTP Server** (the "master clock").  
  * Runs the **Web Application** (Flask server) for the user interface.  
  * Broadcasts "START", "STOP", and "MARK" commands.  
  * Acts as the **Download Station** to pull and sort all video files.  
* **N x Pi Nodes (Clients):** Raspberry Pis (Pi 3B+, 4, or 5\) with cameras.  
  * Sync their internal clocks to the Control Pi's NTP server.  
  * Run a **Listener Script** that waits for commands from the Control Pi.  
  * Execute ``picamera2`` and ``ffmpeg`` to record video and log markers.

## **2\. Hardware Requirements**

* **Control Pi:** 1x Raspberry Pi (Pi 4 or 5 recommended for web server performance).  
* **Pi Nodes:** N x Raspberry Pis (Pi 3B+ or better). One for each camera.  
* **Pi Cameras:** Your v2 and HQ cameras.  
* **Micro SD Cards:** **High-endurance** cards for each Pi (e.g., 64GB or 128GB).  
* **Power Supplies:** Official USB-C power supplies for each Pi.  
* **Network:** A dedicated network router or switch.  
  * **Wired Ethernet is CRITICAL** for this project. Do not use Wi-Fi. It is essential for low-latency command broadcasting and reliable, fast file downloads.

## **3\. Phase 1: Control Pi (Master) Setup**

1. **Install OS:** Install Raspberry Pi OS (Lite) 64-bit.  
2. **Assign Static IP:** Assign a **static IP address** (e.g., ``192.168.1.10``). This IP is the core of the whole system.  
3. **Install NTP Server:**  
   ``sudo apt update && sudo apt install chrony``

4. **Configure NTP Server:** Edit `` /etc/chrony/chrony.conf `` with sudo nano:  
   * Add these lines at the end. This tells chrony to be a local master clock and allow other devices on the network to sync.
~~~
# Allow other clients on the network to sync  
allow 192.168.1.0/24

# Serve time even if not synced to an internet source  
local stratum 10
~~~
5. **Restart & Enable NTP:**  
   ``sudo systemctl restart chrony``  
   ``sudo systemctl enable chrony``

6. **Install Web App Dependencies:**  
   ``sudo apt install rsync python3-pip``  
   ``pip install Flask``

7. **Create Folders:** Create the folder where your web app will live and where videos will be downloaded.  
   ``mkdir ~/pi_controller``  
   ``mkdir ~/video_downloads``

8. **Install Web App:** Copy the ``control_pi_webapp.py`` script into the ``~/pi-controller`` directory.

## **4\. Phase 2: Pi Node (Client) Setup**

**(Repeat these steps for *every* camera Pi)**

1. **Install OS:** Install Raspberry Pi OS (Lite) 64-bit.  
2. **Enable Camera:** Run ``sudo raspi-config``, go to ``Interface Options`` \-\> ``Camera``, and enable it.  
3. **Assign Static IPs:** Assign a **unique static IP address** to each Pi (e.g., ``192.168.1.101``, ``192.168.1.102``, etc.).  
4. **Configure NTP Client:** Tell this Pi to sync its clock *only* to your Control Pi.  
   * Edit ``/etc/systemd/timesyncd.conf`` with ``sudo nano``.  
   * Comment out any existing ``NTP=`` or ``FallbackNTP=`` lines.  
   * Add this line (using your Control Pi's IP):  
     ``NTP=192.168.1.10``

5. **Restart & Verify Sync:** 
   ~~~ 
   sudo systemctl restart systemd-timesyncd  
   sleep 10  
   timedatectl status
    ~~~
   * You should see NTP server: 192.168.1.10 and NTP synchronized: yes. This is a critical step\!  
6. **Create Folders:**  
   ``mkdir ~/pi_listener``  
   ``mkdir ~/videos``

7. **Install Listener Script:** Copy the ``pi_node_listener.py`` script into the ``~/pi_listener`` directory.

## **5\. Phase 3: Passwordless SSH (For ``rsync``)**

The "Download" button uses ``rsync`` over SSH. To avoid it asking for a password every time, you must set up SSH keys.

1. **On the Control Pi (Master):**  
   ~~~
   # Run this ONCE to generate a key. Press Enter at all prompts.  
   ssh-keygen \-t rsa \-b 4096
   ~~~

2. **For EACH Pi Node:**  
   ~~~
   # Run this from the Control Pi, replacing with each node's IP  
   ssh-copy-id pi@192.168.1.101  
   ssh-copy-id pi@192.168.1.102  
   # ... and so on for all nodes
   ~~~
   * You will be asked for the node's password one last time. After this, the Control Pi can log in automatically.

## **6\. Phase 4: Running the System**

1. **Start Listeners:** On *each* **Pi Node**, run:  
   ``python ~/pi_listener/pi_node_listener.py``

2. **Start Web App:** On the **Control Pi**, run:  
   ``python ~/pi_controller/control_pi_webapp.py``

3. Open UI: On your computer (on the same network), open your browser and go to:  
   ``http://192.168.1.10:8080``  
4. **Workflow:**  
   * Type a Take Name (e.g., test\_001\_windy) and click **START**.  
   * Click **MARK** at any time to add a timestamp to the log files.  
   * Click **STOP** to end the recording.  
   * Click **DOWNLOAD & SORT FILES**. The server will download all files in the background and sort them.

## **7\. Post-Production: Your File Structure**

After downloading, your \~/video\_downloads folder on the **Control Pi** will look like this:
~~~
video\_downloads/  
  ├── test\_001\_windy/  
  │   ├── 192.168.1.101/  
  │   │   ├── test\_001\_windy\_192\_168\_1\_101\_20251023\_140210\_000000.h264  
  │   │   ├── test\_001\_windy\_192\_168\_1\_101\_20251023\_140210\_000001.h264  
  │   │   └── test\_001\_windy\_192\_168\_1\_101\_20251023\_140210\_markers.txt  
  │   └── 192.168.1.102/  
  │       ├── test\_001\_windy\_192\_168\_1\_102\_20251023\_140210\_000000.h264  
  │       ├── test\_001\_windy\_192\_168\_1\_102\_20251023\_140210\_000001.h264  
  │       └── test\_001\_windy\_192\_168\_1\_102\_20251023\_140210\_markers.txt  
  │  
  └── another\_take\_002/  
      ├── 192.168.1.101/  
      │   ├── ...  
      └── 192.168.1.102/  
          ├── ...
~~~
All files are synchronized, segmented, and pre-organized for you.

## **8\. Next Step: Automation**

Manually starting the scripts on every boot is tedious. See the automation\_guide.md file for instructions on how to make them start automatically using systemd.