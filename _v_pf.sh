cd /opt/group-brain
systemctl restart group-brain
sleep 3
curl -s -o /dev/null -w "portfolio=%{http_code}\n" http://127.0.0.1:8000/portfolio
curl -s http://127.0.0.1:8000/portfolio | grep -c "全部 12 期的目录"