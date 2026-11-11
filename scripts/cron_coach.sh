#!/bin/bash
# 姣忓懆涓€鐢?cron 璋冭捣鏉ョ殑鍏ュ彛锛氬姞杞?.env锛岀敓鎴愪笂涓€鍛ㄧ殑鍛ㄦ姤銆?#
# 涓轰粈涔堣鍗曠嫭涓€涓剼鏈紝鑰屼笉鏄妸鍛戒护鐩存帴鍐欒繘 crontab锛?#   1. cron 鐨勭幆澧冮噷娌℃湁 DEEPSEEK_API_KEY锛堥偅鏄?systemd 閫氳繃 EnvironmentFile 娉ㄥ叆鐨勶級锛?#      涓?source .env 灏变細鎶ャ€屾病閰?DEEPSEEK_API_KEY锛屽啓涓嶄簡鍛ㄦ姤銆嶏紱
#   2. crontab 閲屽啓 `set -a; . ./.env; set +a; python ...` 杩欑甯﹀垎鍙风殑閾撅紝
#      鍦?Windows 涓?ssh 杩囧幓鏃跺紩鍙蜂細琚墺鎺夛紝鍛戒护琚媶寰楅潰鐩叏闈烇紙韪╄繃锛夈€?#
# 鍑洪敊灏辫 cron 鐨勬棩蹇楅噷鐣欎笅闈為浂閫€鍑猴紝鍒潤榛樺け璐ャ€?set -euo pipefail

cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

WEEK="${1:-last}"
echo "[$(date '+%F %T')] 鐢熸垚鍛ㄦ姤 week=$WEEK"
exec .venv/bin/python scripts/gen_report.py --week "$WEEK"
