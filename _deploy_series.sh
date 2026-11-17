set -e
cd /opt/group-brain
install -m 644 tmp_stage/main.py     app/main.py
install -m 644 tmp_stage/series.py   app/series.py
install -m 644 tmp_stage/series.html web/series.html
mkdir -p out/queue/ep01
install -m 644 tmp_stage/meta.json    out/queue/ep01/meta.json
install -m 644 tmp_stage/desc_fix.json out/queue/desc_fix.json
.venv/bin/python -m py_compile app/main.py app/series.py
systemctl restart group-brain
sleep 3
systemctl is-active group-brain
curl -s -o /dev/null -w "series=%{http_code} " http://127.0.0.1:8000/series
curl -s http://127.0.0.1:8000/api/series | head -c 120; echo
rm -rf tmp_stage