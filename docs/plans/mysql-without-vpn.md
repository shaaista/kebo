# MySQL Access Without VPN — Research & Options

**Problem:** MySQL is at `172.16.5.32:3306` — a private IP only reachable via VPN.  
If the AWS server loses VPN, the entire product goes down.

---

## Option 1 — Install VPN Client on the AWS Server (Easiest)

Instead of your laptop connecting to VPN, make the **AWS server itself** connect to VPN permanently.

**How it works:**
- Install OpenVPN or WireGuard client on the EC2 server
- Server is always on VPN — no manual connection needed
- MySQL stays exactly as it is, nothing changes on the DB side

**Pros:** Quickest fix, no DB migration, no code changes  
**Cons:** If VPN server goes down, product still goes down  
**Security:** VPN encrypts all traffic between EC2 and DB server ✅  
**Ask your developer:** Do you have an OpenVPN or WireGuard config file for the VPN?

---

## Option 2 — Move Database to AWS RDS (Best Long Term)

Create a MySQL database on AWS RDS (Amazon's managed database service) inside the same private network as your EC2 server.

**How it works:**
- EC2 and RDS are in the same AWS VPC (private network)
- They talk to each other directly — no VPN, no internet exposure
- Only your EC2 server can reach the database (via security groups)
- Migrate existing data from `172.16.5.32` to RDS once

**Pros:** Most reliable, fully managed, automatic backups, no VPN dependency  
**Cons:** Costs money (~$15-50/month for small instance), requires data migration  
**Security:** Private VPC — database never exposed to internet ✅  
**Ask your developer:** Can we get AWS RDS access and migrate the database?

---

## Option 3 — Expose MySQL with SSL + IP Whitelist (Quick but Riskier)

Open MySQL port 3306 on the DB server to the internet, but restrict access to only the AWS server's IP and require SSL.

**How it works:**
- DB server opens port 3306 to the internet
- Only your EC2 server's IP is whitelisted in firewall
- All connections use SSL encryption

**Pros:** No VPN needed, no migration  
**Cons:** Database exposed to internet even with whitelist — if EC2 IP changes, breaks; if firewall misconfigured, DB exposed  
**Security:** Weaker than options 1 and 2 — not recommended for production ⚠️  

---

## Recommendation

| Timeline | Option |
|---|---|
| This week (quick fix) | Option 1 — VPN client on EC2 server |
| Next month (proper fix) | Option 2 — Migrate to AWS RDS |

**Do Option 1 now** to remove the manual VPN dependency.  
**Plan Option 2** when you have developer bandwidth — it's the right long-term solution.

---

## What to Ask Your Developer

1. Do we have an OpenVPN/WireGuard config file for the VPN? (For Option 1)
2. Do we have AWS RDS access or budget to create one? (For Option 2)
3. What is the current DB server at `172.16.5.32` — is it our own server or a third-party hosted DB?

---

## Decided: OpenVPN on AWS Server

**We are using OpenVPN.** Plan is to install OpenVPN client on the AWS EC2 server so it stays permanently connected to VPN without anyone's laptop needing to be on VPN.

### What this means
- AWS server connects to VPN automatically on boot
- MySQL at `172.16.5.32:3306` becomes reachable from the server 24/7
- Deployed product works without anyone manually turning on VPN
- Your laptop only needs VPN if you want direct DB access (DBeaver, TablePlus etc.)

### Steps (once developer provides config file)
```bash
# Install OpenVPN
sudo apt install openvpn -y

# Copy the config file to server (developer provides client.ovpn)
sudo cp client.ovpn /etc/openvpn/client.conf

# Enable auto-start on boot and start now
sudo systemctl enable openvpn@client
sudo systemctl start openvpn@client

# Verify VPN is connected
sudo systemctl status openvpn@client

# Test MySQL is reachable
python3 -c "import socket; s=socket.create_connection(('172.16.5.32', 3306), timeout=5); print('MySQL reachable')"
```

### Time needed: 5 minutes (after getting config file)

---

## Message to Send Developer

> Hey, we want to install OpenVPN client directly on our AWS EC2 server so it stays permanently connected to VPN without anyone needing to manually turn on VPN on their laptop. This way the deployed product can reach MySQL at 172.16.5.32:3306 at all times.
>
> Can you give me the `client.ovpn` config file for our VPN? I'll copy it to the server via MobaXterm and set it to auto-start on boot. Should only take 5 minutes once I have the file.
