# MNEMOS Deployment Guide - Bare Metal Linux

**Target**: Linux host (bare metal or VM)
**OS**: Linux (Ubuntu/Debian)
**Python**: 3.10+
**Database**: PostgreSQL 13+

---

## Pre-Deployment Checklist

- [ ] SSH access to your Linux host
- [ ] Python 3.10+ installed
- [ ] PostgreSQL 13+ running on the target host
- [ ] Network access to Graeae (your-host:5001)
- [ ] API credentials for LLM providers (optional)

---

## Step 1: Prepare System

### SSH into the target host

```bash
ssh user@your-host
```

### Verify Python Version

```bash
python3 --version  # Should be 3.10 or higher
```

### Create MNEMOS User (Optional but Recommended)

```bash
# Create dedicated user for MNEMOS
sudo useradd -m -s /bin/bash mnemos

# Add to sudo group if needed
sudo usermod -aG sudo mnemos

# Switch to mnemos user
sudo su - mnemos
```

### Create Application Directory

```bash
# Create directory structure
mkdir -p /opt/mnemos
cd /opt/mnemos

# Clone/copy application code
# (Copy from development machine or clone from git)
```

---

## Step 2: Database Setup

### Create PostgreSQL Database

```bash
# Connect to PostgreSQL
sudo -u postgres psql

# Create database and user
CREATE DATABASE mnemos;
CREATE USER mnemos WITH PASSWORD 'secure_password_here';

# Grant privileges
ALTER ROLE mnemos SET client_encoding TO 'utf8';
ALTER ROLE mnemos SET default_transaction_isolation TO 'read committed';
ALTER ROLE mnemos SET default_transaction_deferrable TO off;
ALTER ROLE mnemos SET default_transaction_read_only TO off;
ALTER ROLE mnemos SET timezone TO 'UTC';

GRANT ALL PRIVILEGES ON DATABASE mnemos TO mnemos;

\q
```

### Run Database Migrations

```bash
# Install psql (if not available)
sudo apt-get install postgresql-client

# Run migrations
psql -h localhost -U mnemos -d mnemos -f db/migrations.sql
```

### Verify Database

```bash
psql -h localhost -U mnemos -d mnemos -c "SELECT count(*) FROM memories;"
```

---

## Step 3: Python Environment Setup

### Create Virtual Environment

```bash
cd /opt/mnemos

# Create venv
python3 -m venv venv

# Activate venv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip setuptools wheel
```

### Install Dependencies

```bash
# Install requirements
pip install -r requirements.txt

# Verify installation
python3 -c "import fastapi; print('FastAPI installed')"
```

---

## Step 4: Configuration

### Create .env File

```bash
cat > /opt/mnemos/.env << 'EOF'
# Database
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=mnemos
DATABASE_USER=mnemos
DATABASE_PASSWORD=secure_password_here

# API Server
MNEMOS_HOST=0.0.0.0
MNEMOS_PORT=5000
MNEMOS_WORKERS=4
MNEMOS_DEBUG=false

# Graeae Integration
GRAEAE_URL=http://your-host:5001
GRAEAE_FALLBACK_ON_ERROR=true
GRAEAE_TIMEOUT_SECONDS=30

# Logging
LOG_LEVEL=INFO
LOG_FILE=/var/log/mnemos/api.log

# LLM Provider Keys (optional)
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk-...
TOGETHER_API_KEY=...
PERPLEXITY_API_KEY=...
EOF

# Restrict permissions
chmod 600 /opt/mnemos/.env
```

### Update config.toml

```bash
# Edit configuration
nano /opt/mnemos/config.toml

# Update database section
[database]
host = "localhost"
port = 5432
database = "mnemos"
user = "mnemos"
password = "secure_password_here"
pool_min_size = 10
pool_max_size = 20
```

---

## Step 5: Systemd Service Setup

### Create Systemd Service File

```bash
sudo cat > /etc/systemd/system/mnemos.service << 'EOF'
[Unit]
Description=MNEMOS API Server
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=notify
User=mnemos
WorkingDirectory=/opt/mnemos
Environment="PATH=/opt/mnemos/venv/bin"
EnvironmentFile=/opt/mnemos/.env
ExecStart=/opt/mnemos/venv/bin/python -m uvicorn api_server:app \
    --host 0.0.0.0 \
    --port 5000 \
    --workers 4 \
    --log-level info

# Auto-restart on failure
Restart=on-failure
RestartSec=10s

# Resource limits
MemoryMax=2G
CPUQuota=80%

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mnemos

[Install]
WantedBy=multi-user.target
EOF
```

### Create Logging Directory

```bash
sudo mkdir -p /var/log/mnemos
sudo chown mnemos:mnemos /var/log/mnemos
```

### Enable and Start Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable mnemos

# Start the service
sudo systemctl start mnemos

# Check status
sudo systemctl status mnemos

# View logs
sudo journalctl -u mnemos -f
```

---

## Step 6: Verification

### Health Check

```bash
# Direct curl
curl -X GET http://your-host:5000/health

# Expected response:
# {"status":"healthy","timestamp":"2026-02-05T...","database_connected":true,"version":"2.0.0"}
```

### Test Memory Creation

```bash
curl -X POST http://your-host:5000/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Test memory content",
    "category": "facts",
    "task_type": "reasoning"
  }'
```

### Check Service Logs

```bash
# Real-time logs
sudo journalctl -u mnemos -f

# Last 50 lines
sudo journalctl -u mnemos -n 50

# Today's logs
sudo journalctl -u mnemos --since today
```

---

## Step 7: Production Hardening

### Setup Firewall

```bash
# Allow API port (only from trusted networks)
sudo ufw allow 5000/tcp from <your-subnet>/24

# Verify
sudo ufw status
```

### Setup Log Rotation

```bash
sudo cat > /etc/logrotate.d/mnemos << 'EOF'
/var/log/mnemos/*.log {
    daily
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 mnemos mnemos
    sharedscripts
    postrotate
        systemctl reload mnemos > /dev/null 2>&1 || true
    endscript
}
EOF
```

### Monitor System Resources

```bash
# Watch service
watch -n 5 'systemctl status mnemos'

# Monitor memory
watch -n 5 'ps aux | grep python'

# Check database connections
psql -h localhost -U mnemos -d mnemos -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='mnemos';"
```

---

## Step 8: Backup Configuration

### Create Backup Script

```bash
cat > /opt/mnemos/backup.sh << 'EOF'
#!/bin/bash

BACKUP_DIR="/opt/mnemos/backups"
mkdir -p $BACKUP_DIR

# Backup database
pg_dump -h localhost -U mnemos mnemos | \
  gzip > $BACKUP_DIR/mnemos-db-$(date +%Y%m%d-%H%M%S).sql.gz

# Backup config
tar -czf $BACKUP_DIR/mnemos-config-$(date +%Y%m%d-%H%M%S).tar.gz \
  /opt/mnemos/config.toml \
  /opt/mnemos/.env

echo "Backup complete"
EOF

chmod +x /opt/mnemos/backup.sh
```

### Schedule Daily Backups

```bash
# Add to crontab
crontab -e

# Add line:
0 2 * * * /opt/mnemos/backup.sh
```

---

## Step 9: Monitoring & Alerting

### Health Monitoring Script

```bash
cat > /opt/mnemos/monitor.sh << 'EOF'
#!/bin/bash

# Check if service is running
systemctl is-active --quiet mnemos
if [ $? -ne 0 ]; then
    echo "ERROR: MNEMOS service is not running"
    # Send alert (email, Slack, etc)
    exit 1
fi

# Check API health
response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/health)
if [ "$response" != "200" ]; then
    echo "ERROR: API health check failed ($response)"
    exit 1
fi

# Check database connection
psql -h localhost -U mnemos -d mnemos -c "SELECT 1" > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "ERROR: Database connection failed"
    exit 1
fi

echo "OK: All checks passed"
exit 0
EOF

chmod +x /opt/mnemos/monitor.sh
```

### Add Monitoring to Crontab

```bash
# Monitor every 5 minutes
*/5 * * * * /opt/mnemos/monitor.sh || systemctl restart mnemos
```

---

## Troubleshooting

### Service Won't Start

```bash
# Check logs
sudo journalctl -u mnemos -n 100

# Test configuration
python3 -m py_compile api_server.py

# Run manually to see errors
cd /opt/mnemos
source venv/bin/activate
python api_server.py
```

### Database Connection Issues

```bash
# Check PostgreSQL status
sudo systemctl status postgresql

# Test connection
psql -h localhost -U mnemos -d mnemos -c "SELECT version();"

# Check user permissions
psql -h localhost -U mnemos -d mnemos -c "\dt"
```

### API Port Already in Use

```bash
# Find process using port 5000
lsof -i :5000

# Kill process (use with caution)
kill -9 <PID>

# Or change port in .env and systemd service
```

### Memory Leaks

```bash
# Monitor memory usage
watch -n 5 'systemctl status mnemos | grep Memory'

# Check detailed memory
ps aux | grep python | grep mnemos

# Restart if needed
sudo systemctl restart mnemos
```

---

## Performance Tuning

### Increase Worker Threads

```bash
# Edit .env
MNEMOS_WORKERS=8  # Increase if CPU allows

# Restart service
sudo systemctl restart mnemos
```

### Adjust Database Pool

```bash
# Edit config.toml
[database]
pool_min_size = 20
pool_max_size = 40

# Restart service
sudo systemctl restart mnemos
```

### Enable Compression on API Responses

```bash
# Add to api_server.py or nginx config
from fastapi.middleware.gzip import GZIPMiddleware
app.add_middleware(GZIPMiddleware, minimum_size=1000)
```

---

## Upgrading MNEMOS

### Steps

```bash
# Stop service
sudo systemctl stop mnemos

# Backup current version
cp -r /opt/mnemos /opt/mnemos.backup

# Update code
# (git pull or copy new files)

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt --upgrade

# Run migrations if database schema changed
psql -h localhost -U mnemos -d mnemos -f db/migrations.sql

# Restart service
sudo systemctl start mnemos

# Verify
curl http://your-host:5000/health
```

---

## Quick Reference Commands

```bash
# Service management
sudo systemctl start mnemos      # Start service
sudo systemctl stop mnemos       # Stop service
sudo systemctl restart mnemos    # Restart service
sudo systemctl status mnemos     # Check status
sudo systemctl enable mnemos     # Enable on boot
sudo systemctl disable mnemos    # Disable on boot

# Logs
sudo journalctl -u mnemos -f     # Follow logs
sudo journalctl -u mnemos -n 100 # Last 100 lines
sudo journalctl -u mnemos --since "10 minutes ago"

# Database
psql -h localhost -U mnemos -d mnemos
\dt                              # List tables
\d memories                       # Describe table
SELECT count(*) FROM memories;   # Count records

# API Testing
curl http://your-host:5000/health
curl http://your-host:5000/stats
curl http://your-host:5000/bundles

# Monitoring
ps aux | grep mnemos             # Check process
lsof -i :5000                    # Check port
top -p $(pgrep -f "python")      # Monitor CPU/Memory
```

---

## Security Considerations

1. **Database Password**: Change from default in `.env`
2. **API Access**: Restrict to trusted networks via firewall
3. **Systemd Service**: Run as unprivileged `mnemos` user
4. **File Permissions**: Ensure `.env` is readable only by `mnemos`
5. **Logs**: Monitor for suspicious activity
6. **Updates**: Keep system and dependencies up to date

---

## Next Steps

1. Deploy to PYTHIA following steps above
2. Monitor service stability for 24-48 hours
3. Setup automated health checks
4. Configure backups and disaster recovery
5. Document any custom configurations
6. Train team on operations procedures

---

**Deployment Complete!**

Your MNEMOS API is now running on PYTHIA at `http://your-host:5000`

For API documentation, see `API_DOCUMENTATION.md`
