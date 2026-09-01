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

# 2026-09-01修TPM超限bug：原本4大類一次打包送1次Groq請求（13,489字元），查證Groq
# 官方文件（console.groq.com/docs/rate-limits，2026-09-01查證）openai/gpt-oss-120b
# 與openai/gpt-oss-20b在Developer/免費tier的限制皆為 8,000 TPM／30 RPM／200,000
# TPD／1,000 RPD——TPM只有8K，即使拆成4類各自查證，個別類別（尤其
# ai_energy_impact 4,243字元、nuclear_country_profiles 4,761字元）仍可能逼近上限，
# 故除了拆成4次獨立請求，呼叫間也加sleep讓TPM滾動視窗重置（見call_groq_diff_one
# 呼叫處註解），而不只看RPM是否寬鬆。
# 每個分類設定：get_data 從status取出該分類監控資料、category_enum限制Groq只能
# 回傳這個分類允許的category值（避免4類合併時的enum混用）、news_labels對應
# fetch_renewable_news()裡的DDG查詢label，讓新聞蒐集也依分類拆分而非4類共用全部新聞
# ——查詢主題本來就跟監控分類一一對應，拆分後既降低單次prompt大小也讓比對更精準。
CATEGORY_CONFIG = [
    {
        'key': 'smart_management',
        'label': '電力管理現況（國際/台灣案例）',
        'get_data': lambda status: status.get('smart_management', {}),
        'category_enum': 'smart_management_intl|smart_management_taiwan',
        'news_labels': ['電力智慧管理案例'],
    },
    {
        'key': 'ai_energy_impact',
        'label': 'AI發展對能源用量的影響',
        'get_data': lambda status: status.get('ai_energy_impact', {}),
        'category_enum': 'ai_energy_impact',
        'news_labels': ['AI能源用量影響'],
    },
    {
        'key': 'nuclear_country_profiles',
        'label': '核能專區（各國營運狀況）',
        'get_data': lambda status: (status.get('nuclear') or {}).get('country_profiles', []),
        'category_enum': 'nuclear_profiles',
        'news_labels': ['核能營運/政策/核廢'],
    },
    {
        'key': 'taiwan_power_shortage',
        'label': '台灣真的缺電嗎',
        'get_data': lambda status: status.get('taiwan_power_shortage', {}),
        'category_enum': 'taiwan_power_shortage',
        'news_labels': ['台灣缺電/備轉容量'],
    },
]


def filter_news_by_label(news_snippets, labels):
    """fetch_renewable_news()每則片段開頭都是「[查詢label] ...」，依CATEGORY_CONFIG
    的news_labels篩出屬於該監控分類的新聞，讓每次Groq請求只帶相關新聞，不是4類共用
    全部新聞（同時降低prompt大小、提升比對精準度）。"""
    prefixes = tuple(f'[{lbl}]' for lbl in labels)
    return [s for s in news_snippets if s.startswith(prefixes)]


def bulletize_email(text):
    """依句號/分號拆句，多於1句就用文字前綴bullet呈現；email HTML相容性考量，
    禁用<ul>/<li>（許多email client如Gmail網頁版會strip掉list-style導致bullet消失），
    見feedback_email_html_compat.md教訓，一律用文字前綴＋<div>換行。"""
    if not text:
        return ''
    parts = [p.strip() for p in re.split(r'(?<=[。；])', text) if p.strip()]
    if len(parts) <= 1:
        return text
    return ''.join(
        f'<div style="margin-top:3px;padding-left:14px;text-indent:-14px;">• {p}</div>'
        for p in parts
    )


def smart_truncate(text, limit=160):
    """硬字元數截斷會切在句子/單詞中間（例如「兩家公」），改成優先在limit內找
    最後一個句號/分號斷開；找不到才退回硬截斷+刪節號，避免文字看起來斷掉。"""
    if not text or len(text) <= limit:
        return text or ''
    cut = text[:limit]
    last_end = max(cut.rfind('。'), cut.rfind('；'))
    if last_end >= limit * 0.4:
        return cut[:last_end + 1]
    return cut.rstrip() + '…'


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


def call_groq_diff_one(category_label, category_data, category_enum, news_snippets):
    """核對「單一監控分類」的資料是否過時，只回報有明確新聞佐證的候選異動。
    2026-09-01改造：原本call_groq_diff一次打包4大類送1個Groq請求，13,489字元的
    prompt遠超Groq免費tier 8,000 TPM上限（官方文件查證見CATEGORY_CONFIG註解），
    連續5輪縮減新聞片段仍必然失敗。現在main()對4大類各自呼叫本函式一次，每次只帶
    單一分類的資料，system prompt/保守原則跟update_nvidia.py同一套設計不變。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是能源產業分析師，任務是核對一份既有的再生能源/核能/AI能源相關結構化參考資料"
        "（這次只給你其中一個監控分類）是否過時。只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣、不提出更新建議；"
        "禁止臆測、禁止捏造來源URL、禁止把不確定的傳聞當成確定事實。"
        "多數週查證後的正確答案就是「沒有任何異動」，回傳空items陣列是完全正常且被期待的結果，"
        "不需要為了顯得有查證成果而硬湊出候選異動。"
        "台灣缺電議題具政治敏感性，若有相關候選異動一律只陳述查證到的事實或官方/各方公開說法，"
        "不下結論不選邊站。"
    )
    def build_prompt(news_list):
        data_json = json.dumps(category_data, ensure_ascii=False, separators=(',', ':'))
        news_text = chr(10).join(news_list) if news_list else '（本週未蒐集到相關新聞片段）'
        return f"""以下是「{category_label}」這個監控分類目前的結構化參考資料現況（JSON，只含
這一個分類，不含其他監控分類與global_mix/historical_trend/renewable_breakdown/carbon等
年度統計數字，那些不在本次查證範圍內，不要對它們提出異動）：

{data_json}

以下是過去一週蒐集到、與此分類相關的新聞片段，每則片段結尾若有「| SOURCE_URL:網址」就是該則
新聞的原始來源網址；source欄位只能填這裡實際出現過的SOURCE_URL，禁止自己編造或憑記憶生成網址：

{news_text}

請核對上述新聞是否讓現有資料的任何欄位過時或不準確，只針對有明確新聞佐證的部分提出候選異動。
不要因為沒有新聞佐證就自己推測任何欄位「應該」要改；找不到能對應到具體新聞的變化就不要提出，
空陣列是完全正常的結果。

輸出格式（純JSON）：
{{
  "items": [
    {{
      "category": "{category_enum}",
      "action": "update|add",
      "target": "若action=update，填現有資料裡對應項目的識別名稱（國家名/案例名稱/欄位名等）；若action=add則留空",
      "fields": {{"要更新或新增的欄位名": "新內容", "...": "..."}},
      "reason": "為何提出這個異動，具體說明新聞依據",
      "source": "新聞來源URL"
    }}
  ],
  "no_change_summary": "若items為空陣列，一句話說明本分類本週查證後判斷現有資料仍準確；若items非空則留空字串"
}}"""
    # 沿用update_nvidia.py既有的413防護模式：413時縮減news_snippets對半重試，最多
    # 縮5輪，5輪都失敗才拋出例外。120b/20b兩個model在免費tier的TPM/RPM完全相同
    # （皆8K TPM/30RPM，2026-09-01查證），這裡保留雙model輪替純粹是取即時可用性
    # 冗餘，不是為了換取更大token額度。單一分類的max_tokens降到2000（原本4類合併
    # 是3000），進一步降低單次請求逼近8K TPM上限的機率。
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
                    max_tokens=2000,
                )
                break
            except GroqAPIStatusError as e:
                if e.status_code == 413:
                    print(f"    → {model} 超出TPM（目前新聞{len(news_list)}則）...")
                    continue
                raise
        if response is not None:
            break
        if not news_list:
            break
        news_list = news_list[:len(news_list)//2]
        print(f"    → 縮減新聞片段至{len(news_list)}則重試...")
    if response is None:
        raise ValueError(f"連續{shrink_round+1}輪（含縮減新聞片段至{len(news_list)}則）仍超出Groq TPM限制")
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
            detail_html = f'<div style="margin-top:5px;padding-left:12px;font-size:12px;color:#6a6460;line-height:1.6;">{bulletize_email(smart_truncate(detail, 160))}</div>' if detail else ''
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
            {bulletize_email(smart_truncate(tps.get('reserve_margin_note') or '', 200))}
          </div>
        </div>'''
    return f'''
    <div style="margin-top:8px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;margin-bottom:4px;">📌 本週監控範圍現況總結</div>
      {blocks}{tps_html}
    </div>'''


def send_email(items, no_change_summary, monitored_snapshot, category_failures=None):
    """獨立信件，收件人只送GitHub Secret設定的NOTIFY_EMAIL。
    category_failures獨立於items/no_change_summary之外渲染，不管items是否為空
    都會顯示，避免「部分類別失敗+其餘類別有候選異動」時失敗訊息被靜默吞掉。"""
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
                f'<div style="font-size:12px;color:#6a6460;margin-top:4px;"><b>{k}：</b>'
                f'{bulletize_email(v) if isinstance(v, str) and len(v) > 40 else v}</div>'
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
                {bulletize_email(it.get("reason",""))}
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
          {bulletize_email(summary)}
        </div>'''
    fail_html = ''
    if category_failures:
        fail_note = '部分類別查證失敗（' + '；'.join(category_failures) + '），其餘分類查證結果如常。'
        fail_html = f'''
        <div style="background:#faf3ea;border-left:3px solid #c08040;padding:12px 16px;border-radius:0 6px 6px 0;font-size:12px;color:#8a5a30;line-height:1.6;margin-bottom:16px;">
          ⚠️ {bulletize_email(fail_note)}
        </div>'''
    body_html = fail_html + changes_html + build_overview_html(monitored_snapshot)

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

    print("🤖 Groq 依4大分類逐一核對是否過時...")
    # 2026-09-01改造：原本1次Groq請求打包4大類（13,489字元）必然超出Groq免費tier
    # 8,000 TPM上限，現在改成4大類各自獨立呼叫call_groq_diff_one，且新聞片段也依
    # CATEGORY_CONFIG.news_labels拆分成該分類專屬的子集，而不是4類共用全部新聞。
    # 單一分類失敗（try/except包住）不會讓其他分類連坐失敗；只有4大類「全部」失敗
    # 才視同過去的全域例外處理（不更新last_checked、寄技術失敗信），否則即使有部分
    # 分類失敗，仍照常寫入其餘分類的查證結果，並在信件裡如實列出哪些分類失敗。
    all_items = []
    no_change_parts = []
    category_failures = []
    for idx, cfg in enumerate(CATEGORY_CONFIG):
        cat_data = cfg['get_data'](status)
        cat_news = filter_news_by_label(news, cfg['news_labels'])
        print(f"  → 查證分類「{cfg['label']}」（新聞{len(cat_news)}則）...")
        try:
            diff = call_groq_diff_one(cfg['label'], cat_data, cfg['category_enum'], cat_news)
            if not isinstance(diff, dict):
                raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
            cat_items = diff.get('items') or []
            all_items.extend(cat_items)
            if not cat_items and diff.get('no_change_summary'):
                no_change_parts.append(diff['no_change_summary'])
            print(f"    → {'偵測到'+str(len(cat_items))+'項候選異動' if cat_items else '無需更新'}")
        except Exception as e:
            print(f"    ⚠ 分類「{cfg['label']}」查證失敗，跳過此分類，其他分類繼續：{e}")
            category_failures.append(f"{cfg['label']}：{e}")
        # 分類間隔sleep：Groq官方文件（2026-09-01查證）RPM=30雖寬鬆，但TPM僅8K/分鐘
        # 才是真正吃緊的限制，即使4次請求本身遠低於RPM上限，累積token量仍可能在同一
        # 60秒滾動視窗內疊加超標，故不只看RPM是否寬鬆，仍在每次呼叫間sleep讓TPM視窗
        # 重置，非最後一類才需要等待。
        if idx < len(CATEGORY_CONFIG) - 1:
            time.sleep(65)

    if len(category_failures) == len(CATEGORY_CONFIG):
        # 4大分類全數查證失敗：視同過去的全域例外處理，不更新last_checked，讓資料
        # 誠實反映「這週其實沒查證成功」。
        fail_summary = '；'.join(category_failures)
        print(f"  ⚠ 4大分類全數查證失敗，本次不更新任何內容：{fail_summary}")
        send_email([], f'本週自動查證因技術問題失敗（{fail_summary}），現有資料未變動，將於下次排程自動重試。', monitored)
        return

    # email只顯示一句固定摘要，不逐類拼接LLM各自產生的「沒變」句子——4句話拼接
    # 常因join用的「；」跟句子本身已有的「。」疊在一起，被bulletize_email拆出只有
    # 「；」的空bullet，而且4個類別各講一次「沒變」對讀者也是純噪音，沒有額外資訊。
    # 各類別的原始no_change_summary仍完整存進pending.json，供人工複核時參考。
    no_change_summary = '本週查證後判斷現有資料仍準確。' if not all_items else ''

    pending = {
        'checked_at': DATE_STR,
        'items': all_items,
        'no_change_summary': no_change_summary,
        'no_change_detail_by_category': no_change_parts,
        'category_failures': category_failures,
    }
    save_json(PENDING_PATH, pending)

    status['weekly_check'] = {'last_checked': DATE_STR}
    save_json(STATUS_PATH, status)

    if all_items:
        print(f"  → 共偵測到 {len(all_items)} 項候選異動，已寫入 data/renewable_pending_review.json（未套用，待人工複核）")
    else:
        print(f"  → 本週查證後無需更新：{no_change_summary}")
    send_email(all_items, no_change_summary, monitored, category_failures=category_failures)
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理，
    # 本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
