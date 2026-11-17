cd /opt/group-brain
.venv/bin/python -m py_compile app/series.py
systemctl restart group-brain
sleep 3
systemctl is-active group-brain
curl -s http://127.0.0.1:8000/api/series | head -c 200
echo