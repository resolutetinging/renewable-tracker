#!/usr/bin/env python3
"""
Renewable Tracker 週查證腳本
獨立於本網站其餘手動更新流程之外——這裡只驗證「案例/政策/現況類」欄位是否過時，
不碰global_mix/historical_trend/renewable_breakdown/carbon這類年度統計數字（那些
本來就只有官方年度報告更新時才會變，週查證去查它們容易產生沒意義的假警報，或
誘導LLM在沒有新一年度統計數字時硬湊「異動」）。設計比照AI Tracker的
scripts/update_nvidia.py（週查證NVIDIA專區），監控範圍：

- smart_management.international / .taiwan（電力智慧化管理案例卡）
- ai_energy_impact.country_profiles（六國AI發展×可用能源狀況）
- nuclear.country_profiles（六國核能營運狀況／營運計畫／政策）
- taiwan_power_shortage（備轉容量率／供需數字／事件記錄／各方說法／風險燈號）

安全設計（鐵律，沿用NVIDIA週查證的既有規範）：本腳本絕不直接改寫
data/renewable_status.json（那是實際顯示在頁面上、看起來權威的參考資料，LLM若把
幻覺內容寫進去會誤導使用者）。所有候選異動一律寫進
data/renewable_pending_review.json，實際套用需要使用者告知Claude人工複核後手動
更新renewable_status.json。
"""
import json, os, re, time, smtplib
from datetime import datetime, timezone, timedelta
from groq import Groq
from groq import APIStatusError as GroqAPIStatusError

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
STATUS_PATH = os.path.join(REPO_DIR, 'data', 'renewable_status.json')
PENDING_PATH = os.path.join(REPO_DIR, 'data', 'renewable_pending_review.json')

CATEGORY_LABELS = {
    'smart_management_intl': '電力管理現況（國際案例）',
    'smart_management_taiwan': '電力管理現況（台灣案例）',
    'ai_energy_impact': 'AI發展對能源用量的影響',
    'nuclear_profiles': '核能專區（各國營運狀況）',
    'taiwan_power_shortage': '台灣真的缺電嗎',
}
ACTION_LABELS = {'update': '更新既有項目', 'add': '新增項目'}

MONITORED_KEYS = ['smart_management', 'ai_energy_impact', 'nuclear_country_profiles', 'taiwan_power_shortage']


def build_monitored_snapshot(status):
    """只把要監控的欄位餵給Groq，不含global_mix/historical_trend/renewable_breakdown/
    carbon這類年度統計資料——避免LLM在沒有新一年度數字時被誘導硬湊異動。"""
    return {
        'smart_management': status.get('smart_management', {}),
        'ai_energy_impact': status.get('ai_energy_impact', {}),
        'nuclear_country_profiles': (status.get('nuclear') or {}).get('country_profiles', []),
        'taiwan_power_shortage': status.get('taiwan_power_shortage', {}),
    }


def fetch_renewable_news():
    """用DDG查最近一週的相關新聞，涵蓋4個監控分類"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("  ⚠ 找不到 ddgs/duckduckgo_search 套件，本次跳過新聞蒐集")
            return []
    queries = [
        ("電力智慧管理案例", "AI datacenter power management deal grid demand response deployment"),
        ("AI能源用量影響", "AI data center electricity demand capacity constraint grid"),
        ("核能營運/政策/核廢", "nuclear power plant construction policy nuclear waste disposal announcement"),
        ("台灣缺電/備轉容量", "台灣 缺電 備轉容量率 供電 電網 台電"),
    ]
    snippets = []
    ddgs = DDGS()
    for label, q in queries:
        for attempt in range(3):
            try:
                results = list(ddgs.news(q, max_results=6, timelimit="w"))
                for r in results:
                    link = r.get('url', '')
                    url_part = f" | SOURCE_URL:{link}" if link else ""
                    snippets.append(f"[{label}] {r.get('title','')} — {r.get('body','')[:200]}{url_part}")
                print(f"  DDG '{label}': {len(results)} results")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    print(f"  DDG '{label}' failed after 3 attempts: {e}")
    return snippets


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def call_groq_diff(monitored_snapshot, news_snippets):
    """核對監控範圍內的資料是否過時，只回報有明確新聞佐證的候選異動。
    system prompt刻意要求極度保守，跟update_nvidia.py同一套設計。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是能源產業分析師，任務是核對一份既有的再生能源/核能/AI能源相關結構化參考資料"
        "是否過時。只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣、不提出更新建議；"
        "禁止臆測、禁止捏造來源URL、禁止把不確定的傳聞當成確定事實。"
        "多數週查證後的正確答案就是「沒有任何異動」，回傳空items陣列是完全正常且被期待的結果，"
        "不需要為了顯得有查證成果而硬湊出候選異動。"
        "台灣缺電議題具政治敏感性，若有相關候選異動一律只陳述查證到的事實或官方/各方公開說法，"
        "不下結論不選邊站。"
    )
    def build_prompt(news_list):
        status_json = json.dumps(monitored_snapshot, ensure_ascii=False, separators=(',', ':'))
        news_text = chr(10).join(news_list) if news_list else '（本週未蒐集到相關新聞片段）'
        return f"""以下是目前監控範圍內的結構化參考資料現況（JSON，只含4個監控分類，
不含global_mix/historical_trend/renewable_breakdown/carbon等年度統計數字，那些
不在本次查證範圍內，不要對它們提出異動）：

{status_json}

以下是過去一週蒐集到的相關新聞片段，每則片段結尾若有「| SOURCE_URL:網址」就是該則新聞的
原始來源網址；source欄位只能填這裡實際出現過的SOURCE_URL，禁止自己編造或憑記憶生成網址：

{news_text}

請核對上述新聞是否讓現有資料的任何欄位過時或不準確，只針對有明確新聞佐證的部分提出候選異動。
不要因為沒有新聞佐證就自己推測任何欄位「應該」要改；找不到能對應到具體新聞的變化就不要提出，
空陣列是完全正常的結果。

輸出格式（純JSON）：
{{
  "items": [
    {{
      "category": "smart_management_intl|smart_management_taiwan|ai_energy_impact|nuclear_profiles|taiwan_power_shortage",
      "action": "update|add",
      "target": "若action=update，填現有資料裡對應項目的識別名稱（國家名/案例名稱/欄位名等）；若action=add則留空",
      "fields": {{"要更新或新增的欄位名": "新內容", "...": "..."}},
      "reason": "為何提出這個異動，具體說明新聞依據",
      "source": "新聞來源URL"
    }}
  ],
  "no_change_summary": "若items為空陣列，一句話說明本週查證後判斷現有資料仍準確；若items非空則留空字串"
}}"""
    # 沿用update_nvidia.py既有的413防護模式：current_status用compact JSON省字元、
    # 413時縮減news_snippets對半重試（不換小model——20b的TPM上限比120b更小，換小
    # model只會更早爆掉），最多縮5輪，5輪都失敗才拋出例外讓main()寄失敗通知信。
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    news_list = list(news_snippets)
    response = None
    shrink_round = 0
    for shrink_round in range(5):
        for model in models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    reasoning_effort="low",
                    messages=[{"role": "system", "content": sys_msg},
                              {"role": "user", "content": build_prompt(news_list)}],
                    temperature=0.2,
                    max_tokens=3000,
                )
                break
            except GroqAPIStatusError as e:
                if e.status_code == 413:
                    print(f"  → {model} 超出TPM（目前新聞{len(news_list)}則）...")
                    continue
                raise
        if response is not None:
            break
        if not news_list:
            break
        news_list = news_list[:len(news_list)//2]
        print(f"  → 縮減新聞片段至{len(news_list)}則重試...")
    if response is None:
        raise ValueError(f"連續{shrink_round+1}輪（含縮減新聞片段至{len(news_list)}則）仍超出Groq TPM限制，監控範圍資料本身可能已過大")
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    finish_reason = response.choices[0].finish_reason
    if finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


def build_overview_html(monitored_snapshot):
    """每週信件固定包含監控範圍現況總結，不管本週有沒有候選異動，比照
    update_nvidia.py的build_overview_html設計。"""
    def case_lines(items):
        rows = ''
        for it in items or []:
            name = it.get('name') or it.get('country') or ''
            date_region = ' · '.join(x for x in [it.get('date'), it.get('region')] if x)
            detail = it.get('desc') or it.get('ai_development_status') or it.get('operating_status') or ''
            date_html = f'<div style="margin-top:4px;font-size:11px;color:#9e9890;">{date_region}</div>' if date_region else ''
            detail_html = f'<div style="margin-top:5px;padding-left:12px;font-size:12px;color:#6a6460;line-height:1.6;">{detail[:160]}</div>' if detail else ''
            rows += f'''<div style="background:#faf9f7;border-left:3px solid #4a8a6a;border-radius:0 6px 6px 0;padding:10px 14px;margin:8px 0;">
              <div style="font-size:13px;font-weight:700;color:#2c2a28;line-height:1.5;">{name}</div>
              {date_html}
              {detail_html}
            </div>'''
        return rows

    sm = monitored_snapshot.get('smart_management', {})
    ai = monitored_snapshot.get('ai_energy_impact', {})
    nuc = monitored_snapshot.get('nuclear_country_profiles', [])
    tps = monitored_snapshot.get('taiwan_power_shortage', {})
    risk = (tps.get('risk_assessment') or {}).get('label', '')

    sections = [
        ('🌐', '電力管理現況（國際）', sm.get('international', [])),
        ('🇹🇼', '電力管理現況（台灣）', sm.get('taiwan', [])),
        ('🤖', 'AI發展對能源用量的影響', ai.get('country_profiles', [])),
        ('☢️', '核能專區（各國營運狀況）', nuc),
    ]
    blocks = ''
    for emoji, label, items in sections:
        n = len(items)
        blocks += f'''
        <div style="margin:16px 0;">
          <div style="font-size:11.5px;font-weight:700;color:#4a8a6a;margin-bottom:6px;">{emoji} {label}（{n}）</div>
          {case_lines(items)}
        </div>'''
    tps_html = f'''
        <div style="margin:16px 0;">
          <div style="font-size:11.5px;font-weight:700;color:#4a8a6a;margin-bottom:6px;">🔌 台灣真的缺電嗎？（現行風險燈號：{risk or '—'}）</div>
          <div style="background:#faf9f7;border-left:3px solid #4a8a6a;border-radius:0 6px 6px 0;padding:10px 14px;margin:8px 0;font-size:12px;color:#6a6460;line-height:1.6;">
            {(tps.get('reserve_margin_note') or '')[:200]}
          </div>
        </div>'''
    return f'''
    <div style="margin-top:8px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;margin-bottom:4px;">📌 本週監控範圍現況總結</div>
      {blocks}{tps_html}
    </div>'''


def send_email(items, no_change_summary, monitored_snapshot):
    """獨立信件，收件人只送GitHub Secret設定的NOTIFY_EMAIL。"""
    user = os.environ.get('GMAIL_USER', '').replace('\xa0', '').replace(' ', '').strip()
    pwd = os.environ.get('GMAIL_APP_PASSWORD', '').replace('\xa0', '').replace(' ', '').strip()
    secret_to = os.environ.get('NOTIFY_EMAIL', user).replace('\xa0', '').replace(' ', '').strip()
    recipients = [a.strip() for a in secret_to.split(',') if a.strip()]

    if not user or not pwd or not recipients:
        print("  → Email 未設定，略過。")
        return

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    subject = f'🌱 Renewable Tracker 概況 {DATE_STR}'
    changes_html = ''
    if items:
        cards = ''
        for it in items:
            cat = CATEGORY_LABELS.get(it.get('category', ''), it.get('category', ''))
            act = ACTION_LABELS.get(it.get('action', ''), it.get('action', ''))
            src = f'<div style="margin-top:6px;font-size:11px;"><a href="{it["source"]}" style="color:#4a8a6a;">來源連結 →</a></div>' if it.get('source') else ''
            fields = it.get('fields') or {}
            fields_html = ''.join(
                f'<div style="font-size:12px;color:#6a6460;margin-top:4px;"><b>{k}：</b>{v}</div>'
                for k, v in fields.items()
            )
            cards += f'''
            <div style="background:#faf9f7;border-left:3px solid #4a8a6a;padding:14px 16px;margin:10px 0;border-radius:0 6px 6px 0;">
              <div style="display:flex;gap:8px;margin-bottom:6px;">
                <span style="font-size:11px;font-weight:700;color:#4a8a6a;background:#4a8a6a18;padding:2px 8px;border-radius:10px;">{cat}</span>
                <span style="font-size:11px;color:#888;">{act}</span>
              </div>
              <div style="font-size:14px;font-weight:700;color:#2c2a28;margin-bottom:6px;">{it.get("target","") or "（新增項目）"}</div>
              {fields_html}
              <div style="background:#f0ede9;border-radius:5px;padding:8px 12px;font-size:12px;color:#6a6460;margin-top:8px;">
                <span style="font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;display:block;margin-bottom:4px;">查證依據</span>
                {it.get("reason","")}
              </div>
              {src}
            </div>'''
        changes_html = f'''
        <div style="margin-bottom:20px;">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;margin-bottom:8px;">🆕 本週偵測到候選異動（待人工複核）</div>
          {cards}
        </div>'''
    else:
        summary = no_change_summary or '本週查證後判斷現有資料仍準確。'
        changes_html = f'''
        <div style="background:#faf9f7;border-left:3px solid #4a8a6a;padding:14px 16px;border-radius:0 6px 6px 0;font-size:13px;color:#4a4744;line-height:1.6;margin-bottom:20px;">
          {summary}
        </div>'''
    body_html = changes_html + build_overview_html(monitored_snapshot)

    html = f'''<html><body style="font-family:'Segoe UI',sans-serif;max-width:620px;margin:auto;padding:0;background:#eceae6;color:#2c2a28;">
      <div style="background:#faf9f7;padding:24px 28px;">
        <div style="border-bottom:1px solid #d8d4ce;padding-bottom:16px;margin-bottom:20px;">
          <div style="font-size:20px;font-weight:800;color:#2c2a28;">🌱 Renewable Tracker 概況</div>
          <div style="font-size:13px;color:#9e9890;margin-top:4px;">{DATE_STR} &nbsp;·&nbsp; 每週自動查證（僅監控案例/政策類欄位）</div>
        </div>
        {body_html}
        <div style="border-top:1px solid #d8d4ce;padding-top:16px;margin-top:20px;text-align:center;">
          <a href="https://resolutetinging.github.io/renewable-tracker/renewable_tracker.html" style="display:inline-block;background:#4a8a6a;color:#fff;padding:10px 24px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;">🔗 查看完整 Dashboard →</a>
          <div style="font-size:11px;color:#b0b0b0;margin-top:12px;">Renewable Tracker · 週查證 · 自動產生 · {DATE_STR}</div>
        </div>
      </div>
    </body></html>'''

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = ','.join(recipients)
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
        print(f"  → Email 已發送至 {', '.join(recipients)}")
    except Exception as e:
        print(f"  → Email 失敗：{e}")


def main():
    print(f"\n{'='*50}")
    print(f"Renewable Tracker 週查證 — {NOW.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    status = load_json(STATUS_PATH, None)
    if status is None:
        print("  ⚠ 找不到 data/renewable_status.json，中止")
        return

    monitored = build_monitored_snapshot(status)

    print("📰 蒐集相關新聞（過去一週）...")
    news = fetch_renewable_news()
    print(f"  → 共 {len(news)} 則片段")

    if not news:
        print("  → 本週未蒐集到任何新聞片段，跳過Groq呼叫（避免無佐證卻要求提出異動）")
        status['weekly_check'] = {'last_checked': DATE_STR}
        save_json(STATUS_PATH, status)
        send_email([], '本週未蒐集到相關新聞片段，僅更新查證時間戳，未進行內容查證。', monitored)
        print("✅ 完成（本週無新聞片段，僅更新查證時間戳）\n")
        return

    print("🤖 Groq 核對監控範圍是否過時...")
    try:
        diff = call_groq_diff(monitored, news)
        if not isinstance(diff, dict):
            raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
    except Exception as e:
        # 比照update_nvidia.py既有教訓：Groq例外也要照樣寄信告知失敗原因，
        # last_checked刻意不更新，讓資料誠實反映「這週其實沒查證成功」，
        # 不可只print就return（那樣job會顯示success但使用者看不出異常）。
        print(f"  ⚠ Groq 呼叫失敗，本次不更新任何內容：{e}")
        send_email([], f'本週自動查證因技術問題失敗（{e}），現有資料未變動，將於下次排程自動重試。', monitored)
        return

    items = diff.get('items') or []
    pending = {
        'checked_at': DATE_STR,
        'items': items,
        'no_change_summary': diff.get('no_change_summary', ''),
    }
    save_json(PENDING_PATH, pending)

    status['weekly_check'] = {'last_checked': DATE_STR}
    save_json(STATUS_PATH, status)

    if items:
        print(f"  → 偵測到 {len(items)} 項候選異動，已寫入 data/renewable_pending_review.json（未套用，待人工複核）")
    else:
        print(f"  → 本週查證後無需更新：{pending['no_change_summary']}")
    send_email(items, pending['no_change_summary'], monitored)
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理，
    # 本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
