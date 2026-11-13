"""多板块「从底部起来」（底部反转）候选面板。

数据接口（挂 /api 前缀，受全局令牌守卫保护）：
  GET /api/screen/bottom-reversal/data?refresh=0|1   返回候选 JSON

面板页（无 /api 前缀，自建 ?token= 校验，避免浏览器无法带 header）：
  GET /screen/bottom-reversal?token=xxx              返回自包含 HTML 面板
"""
from __future__ import annotations

import hmac
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.screener import bottom_reversal as br

# ---- 数据接口：挂 /api 前缀，受全局 _token_guard 保护 ----
api_router = APIRouter(prefix="/screen", tags=["底部反转 · 多板块"])


class BridgeBody(BaseModel):
    """把底部反转候选接入交易链（#191）。默认只入 signals + 自选股（观察/建议链路），
    不主动买卖；``queue=true`` 时进一步送交易队列（受自动交易开关约束）。"""
    top_n: int = 10
    watch: bool = True
    queue: bool = False


@api_router.post("/bottom-reversal/bridge")
def bridge_to_trade(body: BridgeBody) -> dict:
    """将底部反转（多板块）候选写入 signals + 自选股，并可送交易队列。"""
    return br.bridge_to_trade(top_n=body.top_n, watch=body.watch, queue=body.queue)


@api_router.get("/bottom-reversal/data")
def get_candidates(refresh: int = 0):
    """候选数据。refresh=1 强制实时重算（默认读当日缓存）。

    端点刻意声明为同步 ``def``：refresh=1 会遍历数千个日线 parquet 做全量重算，
    FastAPI 会把同步端点放进 threadpool 执行，不会阻塞 event loop（若写成
    ``async def`` 而内部跑同步重活，一次刷新就会让整个 API 卡死数分钟）。

    趋势过滤在读时生效：板块感知——每个板块用自己指数的上升通道判定；若「有数据的
    板块全部处于下降通道」，清空候选并附提示，面板据此展示「暂不给出候选」横幅。
    盘中翻多即解锁候选（无需等收盘）。
    """
    data = br.read_cache(force_refresh=bool(refresh))
    # ---- 趋势指数盘中实时覆写 ----
    # 优先级：缓存里的 trend_index（= 设置项 bottom_trend_index，默认全市场=中证全指 sh000985）
    # > 未设置时退化到旧的多板块感知（主板块官方指数 / 创业板等权合成指数）。
    # 用户要求「分析趋势时全市场一起考虑」，故只要显式设了 trend_index，就直接用它做全市场趋势判定。
    if data.get("trend_filter"):
        from app.config import get_settings as _gs
        _s = _gs()
        raw = (data.get("trend_index") or "").strip()
        if raw:
            # 显式设置了趋势指数（全市场口径）
            sym = raw
            default_name = "中证全指(全市场)" if raw == "sh000985" else raw
            prim = None
        else:
            # 未设置：退化到板块感知（与 twin_t_boards 主板块对齐）
            from app.board_rules import parse_trade_boards
            boards = list(parse_trade_boards(_s.twin_t_boards or "创业板"))
            prim = boards[0] if boards else "cyb"
            if prim in ("cyb", "创业板"):
                sym = None  # 创业板等权合成指数
                default_name = "创业板等权指数"
            else:
                from app.analysis.board_index import board_index_name, board_index_symbol
                sym = board_index_symbol(prim) or None
                default_name = board_index_name(prim) or prim
        try:
            from app.backtest.engine import compute_uptrend_live
            live = compute_uptrend_live(
                sym, int(data.get("trend_ma", 60)),
                kline_dir=str(_s.kline_dir),
            )
            if live.get("uptrend") is not None:
                for bt in data.get("board_trends") or []:
                    if (raw and bt.get("index") == raw) or (prim and bt.get("board") == prim) or (not raw and not prim):
                        bt["uptrend_today"] = live["uptrend"]
                        bt["state"] = "uptrend" if live["uptrend"] else "downtrend"
                data["uptrend_today"] = live["uptrend"]
                data["trend_live_price"] = live.get("live_price")
                data["trend_ma_value"] = live.get("ma_value")
                data["trend_source"] = live.get("source")
                data["trend_index_name"] = live.get("index_name", raw or default_name)
        except Exception:
            pass
    # trend_blocked：趋势过滤开启，且「有数据的板块全部处于下降通道」→ 候选被清空
    avail = [bt for bt in (data.get("board_trends") or []) if bt.get("uptrend_today") is not None]
    data["trend_blocked"] = (
        bool(data.get("trend_filter")) and bool(avail)
        and all(not bt["uptrend_today"] for bt in avail)
    )
    # ---- 统一大盘宏观风险（Layer2：国内大盘 + 外围前一日/当日）----
    try:
        from app.analysis.market_risk import get_unified_market_risk
        data["unified_risk"] = get_unified_market_risk()
    except Exception:
        data["unified_risk"] = None
    # ---- 板块白名单一致性（选股 + 交易已统一到 twin_t_boards，两侧用同一份设置）----
    try:
        from app.board_rules import board_scope_consistency
        from app.config import get_settings as _gs2
        _s2 = _gs2()
        _raw = getattr(_s2, "twin_t_boards", "") or ""
        data["board_consistency"] = board_scope_consistency(_raw, _raw)
    except Exception:
        data["board_consistency"] = None
    # ---- 下降通道横幅 ----
    if data.get("trend_blocked"):
        down = [bt["name"] for bt in (data.get("board_trends") or [])
                if bt.get("uptrend_today") is False]
        msg = (
            "下降通道（" + "、".join(down) + f" ≤ {data.get('trend_ma', 60)} 日 MA），"
            "按「趋势过滤」设置暂不给出底部反转候选；可关闭该开关强制显示。"
        )
        if data.get("trend_source") == "intraday":
            msg += (
                f"（盘中实时：{data.get('trend_live_price')} "
                f"≤ MA {data.get('trend_ma_value')}）"
            )
        data = {**data, "candidates": [], "count": 0, "message": msg}
    return data


# ---- 面板页：无 /api 前缀，自建 query token 校验 ----
router = APIRouter(tags=["底部反转面板 · 多板块"])


def _build_html(token: str) -> str:
    html = _PAGE_TEMPLATE
    return html.replace("__TOKEN__", token)


@router.get("/screen/bottom-reversal")
async def panel_page(request: Request, token: str = ""):
    expected = (get_settings().api_token or "").strip()
    if expected and not hmac.compare_digest(token, expected):
        return JSONResponse(
            status_code=401,
            content={"detail": "需要访问令牌：请用 ?token=xxx 访问本页面"},
        )
    return HTMLResponse(_build_html(token))


_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>底部反转 · 多板块 · SAS</title>
<style>
  :root{--bg:#0f1420;--card:#171d2b;--line:#26304a;--fg:#e6ebf5;--mut:#8a94ad;
        --up:#ff5b5b;--down:#21c08b;--acc:#3da9fc;--gold:#f5c451;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Microsoft YaHei",Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:20px}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--mut);font-size:12px;margin-bottom:16px}
  .bar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
  button{background:var(--acc);color:#04121f;border:0;border-radius:8px;
         padding:7px 14px;font-size:13px;font-weight:600;cursor:pointer}
  button.ghost{background:transparent;color:var(--mut);border:1px solid var(--line)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:14px 16px;margin-bottom:16px}
  canvas{width:100%;height:280px;display:block}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
  th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}
  tbody tr:hover{background:#1d2740}
  .up{color:var(--up)}.down{color:var(--down)}
  .tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px}
  .tag.low{background:rgba(33,192,139,.15);color:var(--down)}
  .tag.high{background:rgba(255,91,91,.15);color:var(--up)}
  .tag.mid{background:rgba(61,169,252,.15);color:var(--acc)}
  .err{color:var(--up)}
  .tag.board{background:rgba(245,196,81,.15);color:var(--gold)}
  .risk{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
  .risk .tier{font-size:15px;font-weight:700;padding:2px 12px;border-radius:8px}
  .tier.calm{background:rgba(33,192,139,.18);color:var(--down)}
  .tier.weak{background:rgba(245,196,81,.18);color:var(--gold)}
  .tier.shock{background:rgba(255,140,0,.18);color:#ff8c00}
  .tier.crisis{background:rgba(255,91,91,.2);color:var(--up)}
  .risk .comp{font-size:12px;color:var(--mut)}
  a{color:var(--acc);text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <h1>底部反转 · 多板块候选面板</h1>
  <div class="sub" id="gen">加载中…</div>
  <div class="sub" id="trend"></div>
  <div class="card" id="scopeWarn" style="display:none;border-color:var(--gold);color:var(--gold);font-size:13px"></div>
  <div class="card" id="riskCard" style="display:none">
    <div class="risk">
      <span class="tier" id="riskTier">-</span>
      <span class="comp" id="riskScore"></span>
      <span class="comp" id="riskComps"></span>
    </div>
    <div class="sub" id="riskBoards" style="margin-top:8px"></div>
  </div>
  <div class="bar">
    <button id="refresh">刷新（实时重算）</button>
    <button class="ghost" id="openData">查看 JSON</button>
  </div>
  <div class="card"><canvas id="chart"></canvas></div>
  <div class="card" style="padding:0;overflow:auto;max-height:60vh">
    <table id="tbl">
      <thead><tr>
        <th>代码</th><th>名称</th><th>板块</th><th>起点位置</th><th>20日高点回撤</th>
        <th>换手率</th><th>20日均额(万)</th><th>最新价</th><th>类别</th><th>14因子分</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="sub">起点位置=近20天前收盘价在近120日价格区间的相对位置（越靠左=越从底部启动）。
  候选标准（收紧口径，避免乱选）：①起点位置≤0.40（低位启动）；②相对20日收盘高点下跌≥15%（处在低位）；
  ③当日换手率≥5%（成交活跃）；④20日均成交额≥5000万（成交量不能太低）。
  ⑤14因子分仅作质量参考列（默认不参与过滤，设置页可开启软闸）；回测显示对底部均值回归策略增益甚微。
  每只票按代码前缀归到所属板块，只用该板块自己的指数判趋势：某板块指数处于下降通道（≤MA）时不给出该板块候选；趋势过滤现已支持盘中实时重算——盘中翻多即解锁候选（无需等收盘）。创业板默认用 300/301 等权合成指数，可在设置页切为官方创业板指399006；其它板块用各自官方指数。</div>
</div>
<script>
const TOKEN="__TOKEN__";
const API="/api/screen/bottom-reversal/data";
function fmtPct(x){return (x>=0?"+":"")+x.toFixed(1)+"%";}
function fmtScore(x){ if(x==null)return "-"; const c=x>=0?"#21c08b":"#ff5b5b"; return '<span style="color:'+c+'">'+x.toFixed(1)+'</span>'; }
function clsTag(c){
  if(c==="底部反转")return '<span class="tag low">底部反转</span>';
  if(c==="高位延续")return '<span class="tag high">高位延续</span>';
  return '<span class="tag mid">中位</span>';
}
function boardName(b){
  return ({"cyb":"创业板","sh_main":"上证主板","sz_main":"深证主板","star":"科创板","bse":"北交所","other":"其他"}[b])||b;
}
function drawChart(rows){
  const cv=document.getElementById("chart");
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth,H=cv.clientHeight;
  cv.width=W*dpr;cv.height=H*dpr;
  const ctx=cv.getContext("2d");ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);
  const padL=44,padB=28,padT=12,padR=12;
  const x0=padL,x1=W-padR,y0=H-padB,y1=padT;
  // 轴
  ctx.strokeStyle="#26304a";ctx.lineWidth=1;ctx.beginPath();
  ctx.moveTo(x0,y1);ctx.lineTo(x0,y0);ctx.lineTo(x1,y0);ctx.stroke();
  ctx.fillStyle="#8a94ad";ctx.font="11px sans-serif";
  // y 轴 (20日高点回撤 0..max)
  const maxDD=Math.max(20,...rows.map(r=>r.dd20_pct||0));
  for(let i=0;i<=4;i++){
    const v=maxDD*i/4, y=y0-(y0-y1)*i/4;
    ctx.fillText(v.toFixed(0)+"%",2,y+3);
    ctx.strokeStyle="#1b2233";ctx.beginPath();ctx.moveTo(x0,y);ctx.lineTo(x1,y);ctx.stroke();
  }
  // x 轴 (起点位置 0..1)
  for(let i=0;i<=5;i++){
    const t=i/5,x=x0+(x1-x0)*t;
    ctx.fillStyle="#8a94ad";ctx.fillText(t.toFixed(1),x-8,y0+14);
  }
  ctx.fillStyle="#8a94ad";ctx.fillText("起点位置（左=底部）",x0,y0+24);
  ctx.fillStyle="#8a94ad";ctx.fillText("20日高点回撤（上=跌得多）",x1-118,y1+2);
  // 点
  rows.forEach(r=>{
    const x=x0+(x1-x0)*Math.min(1,Math.max(0,r.start_pos));
    const y=y0-(y0-y1)*Math.min(1,(r.dd20_pct||0)/maxDD);
    ctx.beginPath();ctx.arc(x,y,4,0,7);
    ctx.fillStyle=r.cls==="底部反转"?"#21c08b":(r.cls==="高位延续"?"#ff5b5b":"#3da9fc");
    ctx.fill();
  });
}
async function load(refresh){
  const url=API+(refresh?"?refresh=1":"");
  try{
    const res=await fetch(url,{headers:{"X-API-Token":TOKEN}});
    if(!res.ok){const e=await res.json().catch(()=>({}));throw new Error(e.detail||res.status);}
    const d=await res.json();
    // 趋势指数名：优先用后端回传（全市场口径=中证全指 sh000985，或板块指数），不再写死「创业板等权指数」
    const bt0=(d.board_trends||[]).length?d.board_trends[0].name:null;
    const idxName=d.trend_index_name
      || (d.trend_index==="sz399006"?"创业板指399006":(d.trend_index?d.trend_index:null))
      || bt0 || "主板块趋势指数";
    const liveExtra=(d.trend_source==="intraday" && d.trend_live_price!=null)
      ? (" · 实时"+d.trend_live_price+" vs MA"+d.trend_ma_value)
      : "";
    const trendTxt=(d.trend_filter?("趋势过滤开("+idxName+")"):"趋势过滤关")
      +"（MA"+d.trend_ma+"）"
      + (d.uptrend_today===true?" · 上升通道":(d.uptrend_today===false?" · 下降通道":""))
      + liveExtra;
    const exitTxt="主动离场："+(d.time_stop_days>0?("持仓"+d.time_stop_days+"日未盈利即时间止损"):"时间止损关")
      +" · "+(d.trailing_pct>0?("涨"+fmtPct(d.trailing_pct*100)+"启动跟踪止损"):"跟踪止损关");
    const fsTxt=(d.criteria&&d.criteria.min_factor_score!=null)?(" · 14因子软闸≥"+d.criteria.min_factor_score):"";
    document.getElementById("gen").textContent=
      "生成时间："+d.generated_at+" · 候选数："+d.count+" · "+trendTxt+" · "+exitTxt+fsTxt;
    // 板块趋势行
    const bt=(d.board_trends||[]).map(function(x){
      const s=x.available===false?"无数据":(x.uptrend_today?"上升":"下降");
      return x.name+"："+s+(x.dev_pct!=null?("("+x.dev_pct.toFixed(1)+"%)"):"");
    }).join(" ｜ ");
    document.getElementById("riskBoards").textContent = bt? ("板块趋势（MA"+d.trend_ma+"）："+bt) : "";
    // 选股板块 vs 交易板块一致性警告（#181）：避免「面板出了候选但买入侧全拒」
    const bc=d.board_consistency;
    const sw=document.getElementById("scopeWarn");
    if(bc && !bc.ok){
      sw.style.display="block";
      sw.textContent="⚠ "+bc.message;
    }else{
      sw.style.display="none";
      sw.textContent="";
    }
    // 统一大盘宏观风险卡（Layer2）
    const ur=d.unified_risk;
    const rc=document.getElementById("riskCard");
    if(ur){
      rc.style.display="block";
      const t=document.getElementById("riskTier");
      const tierZh={"calm":"平稳","weak":"弱势","shock":"震荡","crisis":"危机"}[ur.tier]||ur.tier;
      // calm 档 cap=0 表示「不降仓、沿用总仓位上限」，不能显示成 0%（会误导成清仓）。
      const capTxt=(ur.tier==="calm")?"不降仓（沿用总仓位上限）":("仓位上限 "+(ur.cap*100).toFixed(0)+"%");
      const pauseTxt=(ur.tier==="crisis")?" · 全暂停新开仓买入":"";
      t.textContent="统一大盘："+tierZh+"（"+capTxt+"）"+pauseTxt;
      t.className="tier "+ur.tier;
      document.getElementById("riskScore").textContent=
        "综合风险分 "+ur.score.toFixed(3)+"（国内 "+ur.domestic_score.toFixed(3)+" / 外围 "+ur.overseas_score.toFixed(3)+"）"
        + (ur.overseas_ok?"":" · 外围取数失败(已降级)")
        + (ur.as_of?(" · "+ur.as_of):"");
      const comps=(ur.components||[]).filter(function(c){return c.available!==false;})
        .map(function(c){return c.name+":"+((c.weakness||0)*100).toFixed(1)+"%";}).join("  ");
      document.getElementById("riskComps").textContent=comps;
    }else{
      rc.style.display="none";
    }
    // 下降通道横幅
    const tb2=document.getElementById("trend");
    if(d.message){
      tb2.innerHTML='<span class="err">'+d.message+'</span>';
    }else if(d.trend_blocked){
      tb2.innerHTML='<span class="err">当前为下降通道，暂不给出候选（可在设置关闭趋势过滤）。</span>';
    }else{
      tb2.textContent="";
    }
    const rows=d.candidates||[];
    drawChart(rows);
    const tb=document.querySelector("#tbl tbody");tb.innerHTML="";
    rows.forEach(r=>{
      const tr=document.createElement("tr");
      const ddUp=(r.dd20_pct||0)>=0;
      tr.innerHTML=
        '<td><a href="/stock/'+r.code+'" target="_blank">'+r.code+'</a></td>'+
        '<td>'+r.name+'</td>'+
        '<td><span class="tag board">'+boardName(r.board)+'</span></td>'+
        '<td>'+r.start_pos.toFixed(2)+'</td>'+
        '<td class="'+(ddUp?"down":"up")+'">'+(r.dd20_pct||0).toFixed(1)+'%'+'</td>'+
        '<td>'+fmtPct(r.turnover_pct||0)+'</td>'+
        '<td>'+(r.avg_amount20_wan!=null?r.avg_amount20_wan.toFixed(0):"-")+'</td>'+
        '<td>'+r.last+'</td><td>'+clsTag(r.cls)+'</td><td>'+fmtScore(r.factor_score)+'</td>';
      tb.appendChild(tr);
    });
  }catch(e){
    document.getElementById("gen").innerHTML='<span class="err">加载失败：'+e.message+'</span>';
  }
}
document.getElementById("refresh").onclick=()=>load(true);
document.getElementById("openData").onclick=()=>window.open(API+"?refresh=0","_blank");
load(false);
</script>
</body>
</html>
"""
