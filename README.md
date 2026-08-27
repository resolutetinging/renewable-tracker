# 再生能源追蹤器

靜態參考資料儀表板，追蹤全球能源使用現況、再生能源發展、電力智慧化管理趨勢。

## 架構

- `renewable_tracker.html` — 單檔前端，fetch `data/renewable_status.json` 渲染三個區塊
- `data/renewable_status.json` — 單一事實來源，人工/研究更新，非每日自動產生

## 三個區塊

1. **全球能源使用狀況**：各能源類型全球發電占比排名（Ember Global Electricity Review 年度資料）
2. **再生能源使用狀況**：再生能源內部組成（水力/風力/太陽能）＋整體占比與成長趨勢
3. **電力管理現況 — 智慧化發展**：智慧電網、AI 需量反應、儲能調度等技術與政策最新進展，國際／台灣並列

## v1 範圍

純靜態，資料由 Claude 定期研究更新（IEA / Ember Climate / EIA / 台電 / 經濟部能源署等來源），不接 Groq 自動化驗證。未來若要幫「電力管理現況」區塊接自動化驗證，可參考 aitracker repo 的 NVIDIA 專區週查證機制（`update_nvidia.py`）。

## 更新方式

跟 Claude 說「更新一下再生能源追蹤器」，會重新研究查證最新統計數字與案例，改寫 `data/renewable_status.json`。
