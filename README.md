# PTT C_Chat 每日爬蟲 + LINE 推播

每天 08:00 抓取前一日 00:00~23:59 的 C_Chat 文章，整理熱門文推播到 LINE。

## 1. 安裝

```bash
pip install requests beautifulsoup4 python-dotenv
```

## 2. 設定 LINE

**LINE Notify 已於 2025/3/31 停止服務**，改用 Messaging API：

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 建立 Provider → 建立 **Messaging API channel**
2. 在 **Messaging API** 分頁最下方發行 **Channel access token (long-lived)**
3. 用手機掃該 channel 的 QR code 加自己為好友
4. 取得自己的 User ID：同一頁的 **Your user ID** 欄位（在 Basic settings 分頁），格式為 `U` 開頭的 33 碼

建立 `.env`：

```ini
LINE_CHANNEL_ACCESS_TOKEN=你的_channel_access_token
LINE_USER_ID=U開頭的你的userId

# 以下可選
MIN_PUSH=20        # 推文數門檻
MAX_ITEMS=25       # 訊息最多列幾篇
REQUEST_DELAY=0.4  # 每次請求間隔（秒），請勿調太低
DATA_DIR=./data
```

> 免費方案每月 200 則訊息額度，一天一次日報（1~2 則）綽綽有餘。

## 3. 測試

```bash
python ptt_daily.py --dry-run              # 不推 LINE，只印出來
python ptt_daily.py --date 2026-07-25      # 補抓特定日期
python ptt_daily.py --min-push 50 --limit 10
```

確認沒問題後直接執行 `python ptt_daily.py` 就會推到 LINE。

## 4. 排程：每天 08:00

### Linux / macOS（cron）

```bash
crontab -e
```

加入（請改成你的實際路徑）：

```cron
0 8 * * * cd /home/you/ptt && /usr/bin/python3 ptt_daily.py >> cron.log 2>&1
```

注意 cron 不會讀 `.env` 以外的環境，所以 `cd` 到專案目錄很重要。

### Windows（工作排程器）

1. 工作排程器 → 建立基本工作 → 每天 08:00
2. 動作：啟動程式
   - 程式：`C:\Python312\python.exe`
   - 引數：`ptt_daily.py`
   - 起始位置：`C:\path\to\ptt`

### GitHub Actions（免自己開機）

`.github/workflows/daily.yml`：

```yaml
name: PTT Daily
on:
  schedule:
    - cron: '0 0 * * *'   # UTC 00:00 = 台灣 08:00
  workflow_dispatch:

jobs:
  crawl:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install requests beautifulsoup4
      - run: python ptt_daily.py
        env:
          LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}
          LINE_USER_ID: ${{ secrets.LINE_USER_ID }}
```

GitHub 排程尖峰時段可能延遲 5~20 分鐘，若要求準時請用自己的機器。

## 運作原理

- 從 `index.html`（最新頁）往回翻，用 `上頁` 連結逐頁後退
- 發文時間直接從文章網址 `M.1753712345.A.xxx` 中的 unix timestamp 解析，
  **不需要逐篇進去抓**，一天大約只發 15~25 個請求
- 整頁最新文章都早於目標日 → 停止翻頁
- 抓到的完整清單存成 `data/C_Chat_YYYYMMDD.json`，LINE 只推熱門的部分

## 常見問題

| 狀況 | 處理 |
|---|---|
| 抓到 0 篇 | 先跑 `--dry-run` 看 log，通常是網路或 PTT 改版 |
| LINE 回 400 | User ID 錯誤，或你還沒把該 bot 加好友 |
| LINE 回 401 | Token 過期或貼錯 |
| 想抓別的板 | 改 `BOARD = "C_Chat"` 即可（Gossiping 需要 over18 cookie，程式已內建） |
