/* static/workbench.js — 采编台(拖素材 → 出稿 → 智能辅助 → 三渠道预览)
 *
 * 交互主张(这一版按用户反馈重画):
 *   1) **左栏只有一个口子**: 一个输入框录素材; 素材池里每条素材/每个分句都能拖走。
 *      拖进中栏的素材**从左栏消失**(它是"被用掉了"), 在中栏点 ✕ 就收回左栏。
 *   2) **中栏不预设文章结构**: 拖进来的东西按到达顺序从上往下排, 先到在上;
 *      想调顺序就再拖。没有"导语/背景/过程"这种必须填的槽位 —— 那是渲染层的事。
 *   3) **中栏下半 = 六个角色**: 单选/多选自由组合, 点一下「启动智能辅助」,
 *      右栏上半就逐条滚出各角色的意见; 带「应用」的意见能一键改稿(人在环上)。
 *   4) **右栏下半 = 三渠道实时预览**: 订阅号 / 短视频 / 海报并排, 拖一下就重算。
 *      预览不过核验门禁(还没定稿就该能看样貌), 发布才过门禁。
 *
 * 与后端契约: /api/content/*(六步两相) + /api/content/preview + /api/content/assist
 *             + /api/review/*(采编复盘团队, 走 SSE) + /api/auth/* + /api/user/llm。
 * 零 LLM 可演示: LLM_GATE_MODE=auto_deny 下六角色与三渠道预览都照跑(确定性实现)。
 */
(function () {
  "use strict";

  var CLUB = "demo_club";
  var API = {
    seed: "/api/content/seed_demo",
    analyze: "/api/content/analyze",
    ingest: "/api/content/ingest",
    state: "/api/content/state",
    gaps: "/api/content/gaps",
    checklist: "/api/content/checklist",
    topics: "/api/content/topics",
    draft: "/api/content/draft",
    verify: "/api/content/verify",
    platforms: "/api/content/platforms",
    preview: "/api/content/preview",
    assist: "/api/content/assist",
    assistRoles: "/api/content/assist_roles",
    rollback: "/api/content/rollback",
    versions: "/api/content/versions",
    resolve: "/api/content/resolve_conflict",
    dashboard: "/api/content/dashboard",
    record: "/api/content/review_record",
    reviewExport: "/api/content/review_export",
    packageExport: "/api/content/export_package",
    reviewCreate: "/api/content/review",
    reviewExperts: "/api/review/experts",
    reviewStatus: "/api/review/status",
    reviewInteract: "/api/review/interact",
    reviewHots: "/api/review/hots",
    reviewClose: "/api/review/close",
    reviewStream: "/api/review/stream",
    authMe: "/api/auth/me",
    authLogin: "/api/auth/login",
    authRegister: "/api/auth/register",
    authLogout: "/api/auth/logout",
    userLlm: "/api/user/llm",
    userLlmTest: "/api/user/llm/test"
  };

  var BAND_LABEL = {
    official: "官方文件", organizer: "主办方发布", on_site: "现场记录",
    member: "成员口述", hearsay: "转述"
  };
  var BAND_CLASS = {
    official: "b-official", organizer: "b-organizer", on_site: "b-onsite",
    member: "b-member", hearsay: "b-hearsay"
  };
  var SOURCE_LABEL = {
    official_doc: "官方文件", organizer: "主办方", on_site: "现场记录",
    member: "成员口述", hearsay: "转述"
  };
  var TYPE_ICON = {
    activity: "▣", notice: "▤", person: "☺", achievement: "★", org: "⬢", background: "❖"
  };
  var TYPE_LABEL = {
    activity: "活动", notice: "通知", person: "人物", achievement: "成果",
    org: "组织", background: "背景"
  };
  var KIND_LABEL = {
    conflict: "与素材说法矛盾", isolated: "与已有素材相关", new: "库里没有的新内容",
    detail_of: "属于已有素材", low_confidence: "机器拿不准归属"
  };
  var ACTION_CN = {
    analyze: "素材切句裁决", ingest: "素材入库", seed_demo: "灌演示语料",
    scan_topics: "选题扫描", user_resolve_conflict: "人工裁决矛盾", draft: "成稿",
    redraft_after_resolution: "裁决后重出", verify: "事实核验",
    blocked: "适配被拦下", adapt: "多平台适配", review_record: "生成审核记录",
    rollback: "回滚素材图"
  };
  var SENSITIVE_RE = /(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)|[\u4e00-\u9fff]{2,10}(?:路|街|巷|号院|小区|公寓|宿舍楼)|\d{1,4}\s*室|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;

  // 角色目录的离线兜底(服务端 /api/content/assist_roles 会覆盖它)
  var ROLE_FALLBACK = [
    { id: "fact_checker", label: "事实核查员" },
    { id: "compliance_reviewer", label: "合规风险审查" },
    { id: "copy_editor", label: "文案编辑" },
    { id: "platform_operator", label: "多平台运营" },
    { id: "impact_analyst", label: "传播效果分析师" },
    { id: "review_host", label: "采编主持" }
  ];

  var Doc = window.WBDoc;
  var esc = Doc.esc;
  var USER_KEY = "wb_user_token";

  var state = {
    club: CLUB,
    materials: [],
    matById: {},
    doc: null,
    preview: null,
    verify: null,
    checklist: [],
    roles: ROLE_FALLBACK.slice(),
    sel: {},                 // 选中的角色 id
    ai: [],                  // 右栏消息
    aiQueue: [],
    aiTimer: null,
    drag: null,
    selected: null,          // 选中的积木块
    user: "",                // 登录账号名
    userToken: "",
    llmReady: false,         // 配好了 API Key(决定六角色走团队路线还是确定性路线)
    engine: "",              // "team" | "det"
    // answered = 真有专家给出意见(用于判断"团队到底有没有产出")
    // progress = 任何一条 SSE 事件(用于判断"团队还活着"); 两者不能混, 混了会把
    // "只收到进度事件"当成"已经有意见", 于是全员超时也不降级。
    review: { id: null, es: null, label: {}, active: false, answered: 0, progress: 0,
              watchdog: null, lastEventAt: 0, waiting: 0 },
    analyzeItems: [],
    previewTimer: null
  };

  /* ==================== 基础工具 ==================== */
  function $(id) { return document.getElementById(id); }
  function authToken() { return window.AUTH_TOKEN || window.__AUTH__ || ""; }
  function userToken() {
    if (state.userToken) return state.userToken;
    try { state.userToken = localStorage.getItem(USER_KEY) || ""; } catch (e) { state.userToken = ""; }
    return state.userToken;
  }
  function apiJson(path, opts) {
    var o = opts || {};
    var init = { method: o.method || "GET",
                 headers: { "X-Auth-Token": authToken(), "X-User-Token": userToken() } };
    if (o.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(o.body);
    }
    var url = path;
    if (o.params) {
      var qs = new URLSearchParams(o.params).toString();
      if (qs) url += (url.indexOf("?") >= 0 ? "&" : "?") + qs;
    }
    return fetch(url, init).then(function (res) {
      return res.text().then(function (t) {
        var data = {};
        try { data = t ? JSON.parse(t) : {}; } catch (e) { data = {}; }
        if (!res.ok && !data.error) data.error = (o.label || "请求") + " 失败 (" + res.status + ")";
        return data;
      });
    });
  }
  function apiGet(path, params, label) { return apiJson(path, { params: params, label: label }); }
  function apiPost(path, body, label) {
    return apiJson(path, { method: "POST", body: body || {}, label: label });
  }
  function setStatus(text, kind) {
    var el = $("wb-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "wb-status" + (kind ? " " + kind : "");
  }
  var toastTimer = null;
  function toast(msg, isError) {
    var el = $("wb-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "wb-toast";
      document.body.appendChild(el);
    }
    el.className = "toast" + (isError ? " err" : "");
    el.textContent = msg;
    el.style.display = "block";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.style.display = "none"; }, 3600);
  }
  function isSensitive(m) {
    var badge = (m && m.badge) || {};
    return !!(badge.sensitive || SENSITIVE_RE.test(String((m && m.content) || "")));
  }
  function badgeHtml(band) {
    return '<span class="badge-band ' + (BAND_CLASS[band] || "b-hearsay") + '">' +
      esc(BAND_LABEL[band] || band || "未知来源") + "</span>";
  }
  function glyphOf(m) {
    var key = (m && m.type) || "";
    return '<span class="wb-glyph" title="' + esc(TYPE_LABEL[key] || key) + '">' +
      esc(TYPE_ICON[key] || "▪") + "</span>";
  }
  function nf(n) { return String(n == null ? "-" : n); }

  /* ==================== 左栏：素材池（被用掉的素材不在池里） ==================== */
  /** 中栏用掉了哪些素材: 整条拖走的 eid + 单句拖走的 {eid: {si: true}}。 */
  function consumed() {
    var whole = {}, sents = {};
    (state.doc ? state.doc.blocks : []).forEach(function (b) {
      if (!b.eid) return;
      if (b.whole) { whole[b.eid] = true; return; }
      if (b.si != null && b.si !== "") {
        if (!sents[b.eid]) sents[b.eid] = {};
        sents[b.eid][b.si] = true;
      }
    });
    return { whole: whole, sents: sents };
  }

  function renderMaterials() {
    var box = $("wb-mat-list");
    if (!box) return;
    box.innerHTML = "";
    var use = consumed();
    var shown = 0;
    state.materials.forEach(function (m) {
      if (use.whole[m.eid]) return;                       // 整条被拖进稿子了
      var badge = m.badge || {};
      var all = Doc.splitSentences(m.content);
      var left = all.filter(function (s, i) { return !(use.sents[m.eid] || {})[i]; });
      if (!left.length) return;                           // 分句都被拖走了
      shown += 1;
      var card = document.createElement("div");
      card.className = "wb-mat " + (BAND_CLASS[badge.band] || "b-hearsay");
      card.dataset.eid = m.eid;
      // 来源只显示一次: 权威档位徽章本身已经写了来源通道(官方文件/主办方/现场记录…),
      // 不要再并排一个同义的中文来源标签 —— 那是同一句话说两遍。
      var marks = badgeHtml(badge.band);
      if (badge.period) marks += '<span class="wb-badge-plain">' + esc(badge.period) + "</span>";
      if (badge.verified) marks += '<span class="badge-ok">主办方已确认</span>';
      if (isSensitive(m)) marks += '<span class="badge-alert" title="命中隐私模式">涉隐私</span>';
      var items = left.map(function (s) {
        var i = all.indexOf(s);
        return '<li class="wb-psent" data-eid="' + esc(m.eid) + '" data-si="' + i + '"' +
          ' title="拖到中间，或按回车追加到末尾">' +
          '<span class="wb-psent-txt">' + esc(s) + "</span>" +
          '<span class="wb-psent-grip" aria-hidden="true">⋮⋮</span></li>';
      }).join("");
      card.innerHTML =
        '<div class="wb-mat-head" title="整条拖到中间">' + glyphOf(m) +
        '<span class="wb-mat-subject">' + esc(m.subject || "未命名素材") + "</span>" +
        '<span class="wb-mat-seq">#' + esc(m.seq != null ? m.seq : m.created_seq) + "</span></div>" +
        '<div class="wb-mat-marks">' + marks + "</div>" +
        '<ul class="wb-psents">' + items + "</ul>";
      box.appendChild(card);
    });
    if ($("wb-mat-count")) $("wb-mat-count").textContent = shown + " 条";
    if (!shown && state.materials.length) {
      box.innerHTML = '<div class="wb-empty">素材都拖进这一稿了。' +
        "中栏点 ✕ 就能把某条收回这里。</div>";
    } else if (!state.materials.length) {
      box.innerHTML = '<div class="wb-empty">素材池是空的。把散料粘到上面的框里，' +
        "或者按 ⌘K 灌演示语料。</div>";
    }
  }

  function matCard(eid) {
    return document.querySelector('.wb-mat[data-eid="' + eid + '"]');
  }

  /* ==================== 左栏录入：粘 → 切句 → 入库 ==================== */
  var analyzeTimer = null;
  function onIntakeInput() {
    if (analyzeTimer) clearTimeout(analyzeTimer);
    analyzeTimer = setTimeout(analyze, 700);
  }

  function analyze() {
    var box = $("wb-in-text");
    if (!box) return Promise.resolve();
    var text = (box.value || "").trim();
    if (!text) {
      if ($("wb-in-list")) $("wb-in-list").innerHTML = "";
      state.analyzeItems = [];
      return Promise.resolve();
    }
    setStatus("正在切句裁决…");
    return apiPost(API.analyze, { club_id: state.club, text: text }, "素材裁决")
      .then(function (r) {
        if (r.error) { setStatus(r.error, "err"); return r; }
        state.analyzeItems = (r.items || []).map(function (it) {
          return {
            text: it.text, keep: true,
            verdict: (it.verdict && it.verdict.kind) || "new",
            reason: (it.verdict && it.verdict.reason) || ""
          };
        });
        renderAnalyze();
        setStatus("切出 " + state.analyzeItems.length + " 句，勾掉的不会入库");
        return r;
      }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function renderAnalyze() {
    var box = $("wb-in-list");
    if (!box) return;
    if (!state.analyzeItems.length) { box.innerHTML = ""; return; }
    box.innerHTML = state.analyzeItems.map(function (it, i) {
      return '<label class="wb-in-item' + (it.keep ? " keep" : "") + '">' +
        '<input type="checkbox" data-i="' + i + '"' + (it.keep ? " checked" : "") + " />" +
        '<span class="wb-kind kind-' + esc(it.verdict) + '" title="' +
        esc(KIND_LABEL[it.verdict] || it.verdict) + '">' + esc(it.verdict) + "</span>" +
        '<span class="wb-in-sent">' + esc(it.text) + "</span>" +
        "</label>";
    }).join("");
    box.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
      cb.addEventListener("change", function () {
        var i = parseInt(cb.dataset.i, 10);
        if (state.analyzeItems[i]) state.analyzeItems[i].keep = cb.checked;
        cb.parentElement.classList.toggle("keep", cb.checked);
      });
    });
    var btn = $("wb-ingest-btn");
    if (btn) {
      btn.textContent = "存进去 " + state.analyzeItems.filter(function (x) { return x.keep; }).length + " 句";
    }
  }

  function ingest() {
    var keep = state.analyzeItems.filter(function (x) { return x.keep; });
    if (!keep.length) { setStatus("先粘一段素材，等它切完句", "err"); return Promise.resolve(); }
    var src = ($("wb-in-source") || {}).value || "organizer";
    var mats = keep.map(function (x) { return { content: x.text, source: src }; });
    setStatus("正在入库 " + mats.length + " 句…");
    return apiPost(API.ingest, { club_id: state.club, materials: mats }, "素材入库")
      .then(function (r) {
        if (r.error) { setStatus(r.error, "err"); return r; }
        toast("已存入 " + (r.count || 0) + " 句");
        if ($("wb-in-text")) $("wb-in-text").value = "";
        state.analyzeItems = [];
        renderAnalyze();
        setStatus("已存入素材池：" + (r.count || 0) + " 条，可以拖到中间了", "ok");
        return loadState();
      }).catch(function (e) { setStatus(e.message, "err"); });
  }

  /* ==================== 中栏：积木流 ==================== */
  function boardHook() {
    renderBoard();
    renderMaterials();
    refreshPreview();
  }

  function ensureDoc() {
    if (!state.doc) state.doc = Doc.createDoc({ onChange: boardHook });
    return state.doc;
  }

  function segIdOf(index) { return "blk_" + (index + 1); }
  function indexOfSeg(segId) {
    var m = /^blk_(\d+)$/.exec(segId || "");
    return m ? parseInt(m[1], 10) - 1 : -1;
  }

  function renderBoard() {
    var box = $("wb-board");
    if (!box) return;
    var d = ensureDoc();
    if ($("wb-draft-meta")) {
      $("wb-draft-meta").textContent = d.count() + " 块 · " +
        d.sourcedCount() + " 块有来源" + (d.dirty ? " · 已改" : "");
    }
    if ($("wb-drop-hint")) $("wb-drop-hint").classList.toggle("hidden", !d.isEmpty());
    box.innerHTML = "";
    d.blocks.forEach(function (b, i) {
      box.appendChild(brickEl(b, i));
    });
    if (state.selected) highlightBrick(state.selected.uid);
  }

  function brickEl(b, i) {
    var el = document.createElement("div");
    el.className = "wb-brick" + (b.eid ? " sourced" : " unsourced") +
      (state.selected && state.selected.uid === b.uid ? " active" : "");
    el.dataset.uid = b.uid;
    el.dataset.eid = b.eid || "";
    el.dataset.index = String(i);
    var m = state.matById[b.eid] || {};
    var band = (m.badge && m.badge.band) || b.band || "";
    var refTxt = b.eid ? "来源 " + esc(SOURCE_LABEL[m.source] || b.source || b.eid) : "无来源";
    el.innerHTML =
      '<span class="wb-brick-no">' + (i + 1) + "</span>" +
      '<div class="wb-brick-main">' +
      '<div class="wb-brick-txt">' + esc(b.text) + "</div>" +
      '<div class="wb-brick-foot">' +
      '<button class="wb-brick-ref ' + (BAND_CLASS[band] || "b-hearsay") + '" title="' +
      esc(b.eid ? "点一下：左栏高亮这条素材" : "这块没有来源指针") + '">' + refTxt + "</button>" +
      (b.from === "system" ? '<span class="wb-badge-plain">系统稿</span>' : "") +
      (b.note ? '<span class="wb-brick-note">' + esc(b.note) + "</span>" : "") +
      "</div></div>" +
      '<button class="wb-brick-x" title="从这一稿里拿掉（素材回左栏）">✕</button>';
    el.querySelector(".wb-brick-x").addEventListener("click", function (ev) {
      ev.stopPropagation();
      takeOut(b.uid);
    });
    el.querySelector(".wb-brick-ref").addEventListener("click", function (ev) {
      ev.stopPropagation();
      pickBrick(b);
    });
    return el;
  }

  function takeOut(uid) {
    var d = ensureDoc();
    var gone = d.remove(uid);
    if (!gone) return null;
    if (state.selected && state.selected.uid === uid) state.selected = null;
    setStatus("已从这一稿拿掉 1 块（素材回左栏了）");
    return gone;
  }

  function pickBrick(b) {
    state.selected = { uid: b.uid, text: b.text, eid: b.eid };
    renderBoard();
    var refs = [];
    if (b.eid) {
      var m = state.matById[b.eid] || {};
      refs.push({ eid: b.eid, text: (m.content || "").slice(0, 140),
                  reason: "这一块来自这条素材" });
      var card = matCard(b.eid);
      if (card) {
        card.classList.add("hit");
        card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
    if (window.WBLinks) window.WBLinks.show(refs);
    setStatus(b.eid ? "这一块的来源已在左栏高亮" : "这一块还没有来源指针");
  }

  function highlightBrick(uid) {
    document.querySelectorAll(".wb-brick").forEach(function (el) {
      el.classList.toggle("active", el.dataset.uid === uid);
    });
  }

  function clearPick() {
    state.selected = null;
    document.querySelectorAll(".wb-mat").forEach(function (c) { c.classList.remove("hit"); });
    if (window.WBLinks) window.WBLinks.clear();
    renderBoard();
  }

  /* ==================== 拖拽：素材池 → 积木流（或反过来） ==================== */
  function startDrag(ev, payload) {
    state.drag = {
      payload: payload, startX: ev.clientX, startY: ev.clientY,
      active: false, ghost: null, mark: $("wb-dropmark")
    };
    document.addEventListener("pointermove", onDragMove);
    document.addEventListener("pointerup", onDragUp);
    document.addEventListener("pointercancel", onDragUp);
  }

  function onDragMove(ev) {
    var d = state.drag;
    if (!d) return;
    if (!d.active) {
      if (Math.abs(ev.clientX - d.startX) + Math.abs(ev.clientY - d.startY) <= 5) return;
      d.active = true;
      document.body.classList.add("wb-dragging");
      d.ghost = document.createElement("div");
      d.ghost.className = "wb-ghost";
      d.ghost.textContent = (d.payload.text || "").slice(0, 60);
      document.body.appendChild(d.ghost);
    }
    d.ghost.style.left = (ev.clientX + 12) + "px";
    d.ghost.style.top = (ev.clientY + 14) + "px";
    moveDropMark(ev.clientY);
  }

  /** 落点 → {index:n}（插到第 n 块之前）, {pool:true}, 或 null。 */
  function resolveDrop(x, y) {
    var el = typeof document.elementFromPoint === "function"
      ? document.elementFromPoint(x, y) : null;
    var node = el;
    while (node && node !== document.body) {
      if (node.id === "wb-mat-list" || (node.classList && node.classList.contains("wb-mats"))) {
        return { pool: true };
      }
      if (node.id === "wb-boardwrap") return { index: dropIndexAt(y) };
      if (node.id === "wb-board") return { index: dropIndexAt(y) };
      if (node.classList && node.classList.contains("wb-brick")) {
        var r = node.getBoundingClientRect();
        var idx = parseInt(node.dataset.index, 10) || 0;
        return { index: y < r.top + r.height / 2 ? idx : idx + 1 };
      }
      node = node.parentElement;
    }
    // 元素命中不可用时(离线垫片): 退化成"落点 y 在拖拽区范围内 → 按 y 插"
    var zone = $("wb-boardwrap");
    if (zone && typeof zone.getBoundingClientRect === "function") {
      var zr = zone.getBoundingClientRect();
      if (y >= zr.top && y <= zr.bottom) return { index: dropIndexAt(y) };
    }
    return null;
  }

  function dropIndexAt(y) {
    var bricks = document.querySelectorAll("#wb-board .wb-brick");
    for (var i = 0; i < bricks.length; i++) {
      var r = bricks[i].getBoundingClientRect();
      if (!r || !r.height) continue;
      if (y < r.top + r.height / 2) return i;
    }
    return bricks.length;
  }

  function moveDropMark(y) {
    var mark = state.drag && state.drag.mark;
    if (!mark) return;
    var zone = $("wb-boardwrap");
    if (!zone || typeof zone.getBoundingClientRect !== "function") return;
    var zr = zone.getBoundingClientRect();
    if (y < zr.top || y > zr.bottom) { mark.classList.add("hidden"); return; }
    var bricks = document.querySelectorAll("#wb-board .wb-brick");
    var idx = dropIndexAt(y);
    var top = zr.top + 8;
    if (bricks.length) {
      if (idx < bricks.length) top = bricks[idx].getBoundingClientRect().top - 2;
      else top = bricks[bricks.length - 1].getBoundingClientRect().bottom + 1;
    }
    mark.classList.remove("hidden");
    mark.style.left = zr.left + "px";
    mark.style.top = top + "px";
    mark.style.width = zr.width + "px";
  }

  function onDragUp(ev) {
    var d = state.drag;
    document.removeEventListener("pointermove", onDragMove);
    document.removeEventListener("pointerup", onDragUp);
    document.removeEventListener("pointercancel", onDragUp);
    state.drag = null;
    document.body.classList.remove("wb-dragging");
    if (d && d.ghost && d.ghost.parentElement) d.ghost.parentElement.removeChild(d.ghost);
    var mark = $("wb-dropmark");
    if (mark) mark.classList.add("hidden");
    if (!d || !d.active) return;
    applyDrop(d.payload, resolveDrop(ev.clientX, ev.clientY));
  }

  /** 真正的落点动作(拖拽与测试共用同一条路径)。 */
  function applyDrop(payload, target) {
    if (!target) return null;
    var d = ensureDoc();
    if (target.pool) {
      if (payload.from === "doc") {
        takeOut(payload.uid);
        return target;
      }
      return null;
    }
    if (payload.from === "pool") {
      d.insert(payload.block, target.index);
      setStatus("已插入 1 块（来源 " + (payload.block.eid || "无") + "）");
    } else if (payload.from === "doc") {
      d.move(payload.uid, target.index > d.indexOf(payload.uid)
        ? { index: target.index - 1 } : { index: target.index });
      setStatus("已挪动这一块");
    }
    return target;
  }

  /* ==================== 中栏下半：六个角色 ==================== */
  function renderRoles() {
    var box = $("wb-role-list");
    if (!box) return;
    box.innerHTML = state.roles.map(function (r) {
      var on = !!state.sel[r.id];
      return '<button class="wb-role' + (on ? " on" : "") + '" data-role="' + esc(r.id) +
        '" aria-pressed="' + (on ? "true" : "false") + '" title="' +
        esc(r.desc || r.label) + '">' + esc(r.label) + "</button>";
    }).join("");
    box.querySelectorAll(".wb-role").forEach(function (b) {
      b.addEventListener("click", function () { toggleRole(b.dataset.role); });
    });
    var n = Object.keys(state.sel).filter(function (k) { return state.sel[k]; }).length;
    if ($("wb-role-count")) {
      $("wb-role-count").textContent = n ? n + "/" + state.roles.length + " 已选" : "未选";
    }
  }

  function toggleRole(id) {
    state.sel[id] = !state.sel[id];
    renderRoles();
  }

  function selectedRoles() {
    return state.roles.map(function (r) { return r.id; })
      .filter(function (id) { return state.sel[id]; });
  }

  function loadRoles() {
    return apiGet(API.assistRoles, null, "角色目录").then(function (r) {
      if (r && (r.roles || []).length) state.roles = r.roles;
      renderRoles();
    }).catch(function () { renderRoles(); });
  }

  /* ==================== 右栏上半：AI 消息 ==================== */
  function aiClear(placeholder) {
    var box = $("wb-ai-log");
    if (!box) return;
    box.innerHTML = "";
    if (placeholder) box.innerHTML = '<div class="wb-empty">' + esc(placeholder) + "</div>";
  }

  function aiAppend(msg) {
    var box = $("wb-ai-log");
    if (!box) return null;
    var empty = box.querySelector(".wb-empty");
    if (empty) box.innerHTML = "";
    var el = document.createElement("div");
    el.className = "wb-ai-msg" + (msg.sys ? " sys" : "") + (msg.role ? " role" : "");
    var head = msg.sys ? "" :
      '<div class="wb-ai-head"><span class="wb-ai-who">' + esc(msg.label || msg.role || "系统") +
      '</span><span class="wb-ai-kind">' +
      (msg.kind === "llm" ? "大模型" : msg.kind === "team" ? "团队" : "确定性") + "</span></div>";
    el.innerHTML = head + '<div class="wb-ai-body">' +
      esc(msg.text || "").replace(/\n/g, "<br>") + "</div>";
    if (msg.action && msg.action.label) {
      var row = document.createElement("div");
      row.className = "wb-ai-act";
      var btn = document.createElement("button");
      btn.className = "wb-btn wb-btn-mini";
      btn.textContent = "应用：" + msg.action.label;
      btn.addEventListener("click", function () { applyAiAction(msg, btn); });
      row.appendChild(btn);
      el.appendChild(row);
      msg._btn = btn;
    }
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    msg._el = el;
    return el;
  }

  function pushAi(msg) {
    state.ai.push(msg);
    return aiAppend(msg);
  }

  /** 逐条滚出(打字感的节流, 不是伪造内容: 内容一次算好, 只是分批呈现)。 */
  function revealAi(messages, onDone) {
    state.aiQueue = state.aiQueue.concat(messages || []);
    if (state.aiTimer) return;
    var tick = function () {
      var m = state.aiQueue.shift();
      if (!m) {
        state.aiTimer = null;
        if (onDone) onDone();
        return;
      }
      pushAi(m);
      state.aiTimer = setTimeout(tick, 170);
    };
    state.aiTimer = setTimeout(tick, 60);
  }

  /** 把一条 action 落到积木流上(改动只在这里发生, 用户点才执行)。 */
  function applyAiAction(msg, btn) {
    var d = ensureDoc();
    var a = msg.action || {};
    var acted = 0;
    if (a.type === "drop_blocks") {
      (a.seg_ids || []).slice().sort(function (x, y) {
        return indexOfSeg(y) - indexOfSeg(x);
      }).forEach(function (sid) {
        var i = indexOfSeg(sid);
        if (i >= 0 && d.blocks[i]) { d.remove(d.blocks[i].uid); acted += 1; }
      });
    } else if (a.type === "mask_spans") {
      (a.items || []).forEach(function (it) {
        var i = indexOfSeg(it.seg_id);
        if (i >= 0 && d.blocks[i]) { d.update(d.blocks[i].uid, { text: it.text, note: "已脱敏" }); acted += 1; }
      });
    } else if (a.type === "split_blocks") {
      (a.items || []).slice().reverse().forEach(function (it) {
        var i = indexOfSeg(it.seg_id);
        var b = i >= 0 ? d.blocks[i] : null;
        if (!b || !(it.pieces || []).length) return;
        var pieces = it.pieces.map(function (p) {
          return { text: p, eid: b.eid, si: b.si, whole: b.whole, band: b.band,
                   source: b.source, from: b.from, note: "拆句" };
        });
        d.remove(b.uid);
        d.insertMany(pieces, Math.min(i, d.count()));
        acted += 1;
      });
    } else if (a.type === "trim_to") {
      var keep = parseInt(a.keep, 10);
      if (keep > 0) {
        d.blocks.slice(keep).map(function (b) { return b.uid; }).forEach(function (uid) {
          d.remove(uid); acted += 1;
        });
      }
    }
    if (!acted) { setStatus("这条改动已经不需要了（稿件变了）", "err"); return; }
    if (btn) { btn.textContent = "已应用"; btn.disabled = true; }
    msg.applied = true;
    // 改完之后, 其他还没应用的建议按老位置失效 —— 明说, 不装作还能用
    state.ai.forEach(function (m) {
      if (m !== msg && m.action && !m.applied && m._btn) {
        m.stale = true;
        m._btn.textContent = "稿件已变，重新跑一次";
        m._btn.disabled = true;
      }
    });
    pushAi({ sys: true, text: "已应用「" + a.label + "」，改了 " + acted +
      " 处。预览已刷新，可以再跑一次辅助看剩下的问题。" });
    setStatus("已应用改动 " + acted + " 处");
    renderBoard();
  }

  /* ==================== 六角色辅助 ====================
   * 两条路线, 由「有没有配好 API」决定走哪条(界面上会写明是哪条):
   *
   *   ① 团队路线(有 Key): /api/content/review 起一场真复盘 —— SwarmFlow 多智能体,
   *      按勾选的角色逐个点名发言, 走 SSE 流式滚进右栏。这是规格里的「智能体相」。
   *   ② 确定性路线(没 Key / 团队起不来): /api/content/assist —— 同一批角色, 判断
   *      全部由确定性代码得出, 零 LLM、结论可复现, 并且带可一键应用的改稿 action。
   *
   * 团队路线**失败就降级**, 不让人卡在一个转圈的按钮上: 我们会明说"改走确定性版本"。
   */
  function resetAiLog() {
    state.aiQueue = [];
    state.ai = [];
    if (state.aiTimer) { clearTimeout(state.aiTimer); state.aiTimer = null; }
  }

  function startAssist() {
    var d = ensureDoc();
    var roles = selectedRoles();
    if (!roles.length) { setStatus("先在中栏下面勾一个角色（可以多选）", "err"); return Promise.resolve(); }
    if (d.isEmpty()) { setStatus("中间还是空的：先把左边素材拖进来", "err"); return Promise.resolve(); }
    resetAiLog();
    if (state.review.id) closeReviewSession();          // 上一场还没关就先关掉
    if (state.llmReady) return startTeamAssist(roles);
    return runDeterministicAssist(roles, true);
  }

  /** ① 团队路线: 起一场真复盘(API 多智能体), 意见由 SSE 滚进右栏。 */
  /**
   * 团队路线: 起一场真复盘(API 多智能体), 意见由 SSE 滚进右栏。
   *
   * 计时口径(踩过坑, 别改回去): 团队"看起来没动静"是**正常**的——
   *   起团队(几秒) → 主持注册名册(一次 LLM 往返) → 逐个点名专家,
   *   每个专家还要先读素材库/待核实清单/稿件三个工具才开口。
   * 所以这里等的不是"多久出第一条", 而是"**多久没有任何一条事件**":
   *   - 任何 SSE 事件(history/answer/team_message/message/hots_*)都算进展, 重置计时;
   *   - 静默窗口(默认 90s)必须大于后端单专家的等待上限(60s), 否则会在专家正写着的时候抢跑。
   * 上一版是"发起后 25 秒没产出就降级", 结果把正常启动的团队判成失败并关掉它 —— 那是错的。
   */
  var TEAM_SILENCE_MS = 90000;

  function startTeamAssist(roles) {
    state.engine = "team";
    aiClear("正在按你选的角色起采编复盘团队（走 API 的多智能体）…");
    if ($("wb-ai-state")) $("wb-ai-state").textContent = "团队(API) 启动中";
    setStatus("正在起复盘团队（" + roles.length + " 个角色，走 API）…团队启动要几十秒，属于正常");
    return apiPost(API.reviewCreate, { club_id: state.club, roles: roles }, "开复盘")
      .then(function (r) {
        if (r.error) return teamFallback("团队起不来：" + r.error, true);
        state.review.id = r.review_id || null;
        state.review.answered = 0;
        state.review.progress = 0;
        state.review.waiting = 0;
        if ($("wb-ai-state")) {
          $("wb-ai-state").textContent = "团队(API) " + (r.review_id || "");
        }
        rvOpenStream();
        armTeamWatchdog();
        return r;
      }).catch(function (e) { return teamFallback("团队请求失败：" + e.message, true); });
  }

  /** 重置"久无进展"计时。任何一条 SSE 事件都应调用它。 */
  function armTeamWatchdog() {
    if (state.review.watchdog) clearTimeout(state.review.watchdog);
    state.review.lastEventAt = Date.now();
    state.review.watchdog = setTimeout(teamSilenceCheck, TEAM_SILENCE_MS);
  }

  function teamSilenceSeconds() {
    if (!state.review.lastEventAt) return Math.round(TEAM_SILENCE_MS / 1000);
    return Math.round((Date.now() - state.review.lastEventAt) / 1000);
  }

  /**
   * 静默到点了: 先问一次会话状态, 别把"还在写"当成"死了"。
   * 团队还活着(ready / 有专家在工作) → 只提示一句, 继续等下一窗口;
   * 团队确实没有起色 → 才降级(且**不**关会话, 后面真出结果还能滚进来)。
   */
  function teamSilenceCheck() {
    var secs = teamSilenceSeconds();
    if (!state.review.id) return;
    apiGet(API.reviewStatus, { review_id: state.review.id }, "会话状态").then(function (s) {
      var busy = s && !s.error && (s.ready || s.status === "ready" ||
        s.status === "starting" || s.status === "running");
      var working = ((s && s.experts) || []).filter(function (e) {
        return e && e.status === "working";
      }).length;
      if (busy || working) {
        state.review.waiting = (state.review.waiting || 0) + 1;
        pushAi({ sys: true, text: "团队还在写（已经 " + secs + " 秒没有新消息" +
          (working ? "，" + working + " 个专家工作中" : "") +
          "）。多智能体逐个发言本来就慢，继续等。" });
        if ($("wb-ai-state")) $("wb-ai-state").textContent = "团队(API) 撰写中…";
        return armTeamWatchdog();
      }
      return teamFallback("团队 " + secs + " 秒没有任何进展", false);
    }).catch(function () {
      // 状态都查不到, 就不再猜了: 降级但保留会话(万一后面真有输出)
      return teamFallback("团队 " + secs + " 秒没有任何进展", false);
    });
  }

  /**
   * 团队路线失败 → 明说原因, 然后走确定性版本(不静默、不重复撞墙)。
   * closeSession: 只有"根本起不来"才关; "没产出"这种保留会话, 后面真出结果还能滚进来。
   */
  function teamFallback(reason, closeSession) {
    if (state.review.watchdog) {
      clearTimeout(state.review.watchdog);
      state.review.watchdog = null;
    }
    if (closeSession) {
      pushAi({ sys: true, text: reason + "。改走确定性版本（不调 API，结论一样可复现）。" });
      state.llmReady = false;          // 起不来就别再撞同一堵墙; 设置里改好 Key 会重新置位
      if (state.review.es) { state.review.es.close(); state.review.es = null; }
      if (state.review.id) {
        var rid = state.review.id;
        state.review.id = null;
        apiPost(API.reviewClose, { review_id: rid }, "结束会话").catch(function () { return {}; });
      }
    } else {
      // 保留会话: SSE 不动, 团队若真的接着产出, 消息会继续滚进右栏
      pushAi({ sys: true, text: reason + "。先给你确定性版本（不调 API，结论一样可复现）；" +
        "团队还在后台跑，真出结果会继续滚上来。" });
      if (state.review.es) { state.review.es.close(); state.review.es = null; }
    }
    var roles = selectedRoles();
    if (!roles.length) return Promise.resolve();
    return runDeterministicAssist(roles, false);
  }

  /**
   * 个别角色没拿到意见: 只要**已经有角色真的给出意见**就不降级(半份意见也比没有强);
   * 一个都还没有 → 降级到确定性版本(否则用户会空着等)。
   * 注意这里看的是 answered 而不是 progress: 只收到进度事件不算有产出。
   */
  function teamMaybeFallback(why) {
    if (state.review.answered) {
      pushAi({ sys: true, text: why + " —— 其余角色已经给出意见，不再降级。" });
      return Promise.resolve();
    }
    return teamFallback(why, false);
  }

  /** ② 确定性路线: 同一批角色, 零 LLM, 意见里带可一键应用的 action。 */
  function runDeterministicAssist(roles, clearLog) {
    var d = ensureDoc();
    if (clearLog) aiClear("正在按你选的角色看这一稿…");
    state.engine = "det";
    if ($("wb-ai-state")) $("wb-ai-state").textContent = "确定性 运行中…";
    setStatus("智能辅助运行中（" + roles.length + " 个角色，确定性实现）…");
    return apiPost(API.assist, {
      club_id: state.club, segments: d.toSegments(), roles: roles
    }, "智能辅助").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); if ($("wb-ai-state")) $("wb-ai-state").textContent = "出错"; return r; }
      var msgs = (r.messages || []).map(function (m) {
        return { role: m.role, label: m.label, kind: m.kind || "deterministic",
                 text: m.text, action: m.action || null };
      });
      revealAi(msgs, function () {
        if ($("wb-ai-state")) $("wb-ai-state").textContent = "确定性 " + msgs.length + " 条意见";
        setStatus("辅助完成：" + msgs.length + " 条意见（可应用的会有按钮）", "ok");
      });
      return r;
    }).catch(function (e) {
      setStatus(e.message, "err");
      if ($("wb-ai-state")) $("wb-ai-state").textContent = "出错";
    });
  }

  function askAi() {
    var input = $("wb-ai-input");
    if (!input) return;
    var text = (input.value || "").trim();
    if (!text) return;
    input.value = "";
    if (state.review.id) {                     // 团队会话在跑 → 转给团队(真 LLM)
      pushAi({ role: "human_agent", label: "你", kind: "team", text: text });
      apiPost(API.reviewInteract, { review_id: state.review.id, body: text }, "发送")
        .catch(function (e) { pushAi({ sys: true, text: "发送失败：" + e.message }); });
      return;
    }
    // 没开团队会话: 按"点名"处理 —— 命中角色名就只跑那个角色
    var hit = state.roles.filter(function (r) {
      return text.indexOf(r.label) >= 0 || r.id === text;
    })[0];
    pushAi({ role: "human_agent", label: "你", kind: "deterministic", text: text });
    if (!hit) {
      var names = state.roles.map(function (r) { return r.label; }).join("、");
      pushAi({ sys: true, text: "这句话我按确定性规则答不了。可以点上面的角色按钮直接跑，" +
        "或者在话里带上角色名点名（" + names + "）。要走真大模型的团队复盘，用 ⌘K → 「采编复盘」。" });
      return;
    }
    var d = ensureDoc();
    apiPost(API.assist, { club_id: state.club, segments: d.toSegments(), roles: [hit.id] },
      "点名").then(function (r) {
        revealAi((r.messages || []).map(function (m) {
          return { role: m.role, label: m.label, kind: m.kind, text: m.text, action: m.action || null };
        }));
      }).catch(function (e) { pushAi({ sys: true, text: "失败：" + e.message }); });
  }

  /* ==================== 右栏下半：三渠道实时预览 ==================== */
  function refreshPreview() {
    if (state.previewTimer) clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(function () {
      var d = ensureDoc();
      if (d.isEmpty()) { state.preview = null; renderPreview(); return; }
      apiPost(API.preview, { segments: d.toSegments() }, "预览").then(function (r) {
        if (r.error) { setStatus(r.error, "err"); return; }
        state.preview = r;
        renderPreview();
      }).catch(function () { renderPreview(); });
    }, 220);
  }

  function renderPreview() {
    var order = ["wechat", "shortvideo", "poster"];
    var PV_IDS = { wechat: "wb-pv-wechat", shortvideo: "wb-pv-shortvideo", poster: "wb-pv-poster" };
    var pv = state.preview && state.preview.channels;
    order.forEach(function (key) {
      var card = $(PV_IDS[key]);
      if (!card) return;
      if (!pv || !pv[key]) {
        card.innerHTML = '<div class="wb-pv-head">' + esc(PLAT_LABEL[key]) + "</div>" +
          '<div class="wb-pv-body"><div class="wb-empty">拖素材进来就有预览</div></div>';
        return;
      }
      card.innerHTML = pvHead(pv[key]) + '<div class="wb-pv-body">' + pvBody(key, pv[key]) + "</div>" +
        pvFoot(pv[key]);
    });
    if ($("wb-pv-state")) {
      var st = (state.preview && state.preview.stats) || {};
      $("wb-pv-state").textContent = st.blocks ? (st.blocks + " 块 · " + st.chars + " 字") : "实时";
    }
  }
  var PLAT_LABEL = { wechat: "订阅号", shortvideo: "短视频", poster: "海报" };

  function pvHead(c) {
    var lim = c.limit || {};
    var over = lim.over ? " over" : "";
    return '<div class="wb-pv-head">' + esc(c.label) +
      '<span class="wb-pv-count' + over + '">' + nf(lim.count) + "/" + nf(lim.limit) + "</span></div>";
  }

  function pvBody(key, c) {
    if (key === "poster") {
      return '<div class="wb-poster">' + (c.parts || []).map(function (p) {
        var empty = !p.text;
        return '<div class="wb-poster-slot' + (empty ? " empty" : "") + '">' +
          '<span class="wb-poster-label">' + esc(p.label) + "</span>" +
          '<span class="wb-poster-text' + (p.slot === "headline" ? " big" : "") + '">' +
          esc(empty ? "（素材里没找到）" : p.text) + "</span></div>";
      }).join("") + "</div>";
    }
    if (key === "shortvideo") {
      return '<div class="wb-shots">' + (c.parts || []).map(function (p) {
        var empty = !p.text;
        return '<div class="wb-shot' + (empty ? " empty" : "") + '">' +
          '<span class="wb-shot-no">' + esc(p.label) + "</span>" +
          '<span class="wb-shot-txt">' + esc(empty ? "（还没素材）" : p.text) + "</span></div>";
      }).join("") + "</div>";
    }
    return '<div class="wb-pv-title">' + esc(c.title) + "</div>" +
      (c.parts || []).map(function (p) {
        return '<p class="wb-pv-seg">' + esc(p.text) + "</p>";
      }).join("");
  }

  function pvFoot(c) {
    var notes = (c.notes || []).map(function (n) {
      return '<div class="wb-pv-note">' + esc(n) + "</div>";
    }).join("");
    return notes ? '<div class="wb-pv-foot">' + notes + "</div>" : "";
  }

  function renderPlatforms() { renderPreview(); }
  function setPlatform() { renderPreview(); }
  function highlightPlatform() { /* 预览已常驻, 不再需要切换 */ }

  /* ==================== 后端动作 ==================== */
  function loadState() {
    return apiGet(API.state, { club_id: state.club }, "素材图").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      state.materials = r.materials || [];
      state.matById = {};
      state.materials.forEach(function (m) { state.matById[m.eid] = m; });
      if ($("wb-seq-chip")) {
        $("wb-seq-chip").textContent = "seq " + nf(r.head_seq);
      }
      renderMaterials();
      renderBoard();
      return r;
    });
  }

  function loadChecklist() {
    return apiGet(API.checklist, { club_id: state.club }, "待核实清单").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      state.checklist = r.check_list || [];
      renderChecklist(r.summary || {});
      return r;
    });
  }

  function renderChecklist(summary) {
    var box = $("wb-cd-body");
    if (!box) return;
    if ($("wb-cd-sum")) {
      $("wb-cd-sum").textContent = "共 " + nf(summary.total) + " 条 · 阻断 " +
        nf(summary.blocking) + " 条";
    }
    if (!state.checklist.length) {
      box.innerHTML = '<div class="wb-empty">没有待核实项。素材与稿件目前是一致的。</div>';
      return;
    }
    box.innerHTML = state.checklist.map(function (it) {
      var cands = it.candidates || [];
      var chosen = it.chosen_eid || "";
      var candHtml = cands.map(function (c) {
        var adopted = chosen && c.eid === chosen;
        var cm = state.matById[c.eid] || {};
        var cband = (cm.badge && cm.badge.band) || "";
        return '<div class="wb-cand' + (adopted ? " adopted" : "") + '">' +
          '<div class="wb-cand-top"><span class="wb-cand-val">' +
          esc(String(c.value) + (c.unit || "")) + "</span>" + badgeHtml(cband) +
          '<span class="wb-hint">' + esc(SOURCE_LABEL[c.source] || c.source || "-") + "</span>" +
          (adopted ? '<span class="badge-adopted">当前采用·' +
            (it.resolution === "user" ? "你选的" : "按来源排的") + "</span>" : "") + "</div>" +
          '<div class="wb-cand-text">' + esc(c.text || "") + "</div>" +
          '<div class="wb-cand-foot">' + (adopted
            ? '<span class="wb-hint">' + esc(it.resolution_note || "已采用") + "</span>"
            : '<button class="wb-resolve" data-eid="' + esc(c.eid) + '" data-seg="' +
              esc(it.seg_id || "") + '">选它</button>') + "</div></div>";
      }).join("");
      return '<div class="wb-cd-item' + (it.blocking ? " blocking" : "") + '">' +
        '<div class="wb-cd-top"><span class="wb-gap-tag g-' + esc(it.weakness_type) + '">' +
        esc(it.label) + "</span>" +
        (it.blocking ? '<span class="badge-alert">阻断·不许进多平台</span>' : "") +
        '<span class="wb-cd-src">' + esc(it.source_eid || "无来源") + "</span></div>" +
        '<div class="wb-cd-assert">' + esc(it.assertion || "") + "</div>" +
        '<div class="wb-cd-why">' + esc(it.reason || "") + "</div>" +
        (cands.length ? '<div class="wb-cd-cands">' + candHtml + "</div>" : "") + "</div>";
    }).join("");
    box.querySelectorAll("button.wb-resolve").forEach(function (b) {
      b.addEventListener("click", function () { resolveConflict(b.dataset.eid, b.dataset.seg); });
    });
  }

  function openChecklist() {
    if ($("wb-cd-drawer")) $("wb-cd-drawer").classList.remove("hidden");
    if ($("wb-cd-mask")) $("wb-cd-mask").classList.remove("hidden");
    return loadChecklist();
  }
  function closeChecklist() {
    if ($("wb-cd-drawer")) $("wb-cd-drawer").classList.add("hidden");
    if ($("wb-cd-mask")) $("wb-cd-mask").classList.add("hidden");
  }

  function resolveConflict(eid, segId) {
    return apiPost(API.resolve, { club_id: state.club, chosen_eid: eid, seg_id: segId || "" },
      "裁决").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      toast("已按你选的口径重出稿件" + (r.redraft && r.redraft.seq != null
        ? "（seq=" + r.redraft.seq + "）" : ""));
      return loadChecklist().then(loadState);
    }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function seedDemo() {
    setStatus("正在灌演示语料…");
    return apiPost(API.seed, { club_id: state.club }, "灌语料").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      toast("已灌入 " + (r.count || 0) + " 条素材（历史层 3 + 当年层 8）");
      return loadState();
    }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function autoDraft() {
    setStatus("让系统先出一版…");
    return apiPost(API.draft, { club_id: state.club, max_segments: 6 }, "成稿")
      .then(function (r) {
        if (r.error) { setStatus(r.error, "err"); return r; }
        ensureDoc().fromDraft(r.draft);
        state.verify = null;
        renderBoard();
        renderMaterials();
        refreshPreview();
        setStatus("系统先出了 " + (r.draft.segments || []).length +
          " 块；拖着你改，或者勾角色让它帮你看", "ok");
        return r;
      }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function verify() {
    var d = ensureDoc();
    if (d.isEmpty()) { setStatus("中间还是空的：先拖素材进来", "err"); return Promise.resolve(); }
    setStatus("正在核验这一稿…");
    return apiPost(API.verify, { club_id: state.club, segments: d.toSegments() }, "核验")
      .then(function (r) {
        if (r.error) { setStatus(r.error, "err"); return r; }
        state.verify = r.verify;
        d.markVerify(r.verify);
        renderBoard();
        renderChecklist((r.verify && r.verify.summary) || {});
        setStatus(r.block
          ? "有阻断项：不许进多平台。⌘K → 待核实清单看是哪几块"
          : "核验通过：可以发布了", r.block ? "err" : "ok");
        if (r.block) openChecklist();
        return r;
      }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function adapt() {
    if (!state.verify) {
      setStatus("先核验再发布（带阻断项的稿件不许进多平台）", "err");
      return Promise.resolve();
    }
    return apiPost(API.platforms, { club_id: state.club }, "多平台适配").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      if (r.blocked) {
        setStatus("发布被拦下：" + (r.reason || "存在阻断项"), "err");
        openChecklist();
        return r;
      }
      setStatus("已出 " + Object.keys(r.platforms || {}).length + " 个平台版本（可导出宣发包）", "ok");
      return r;
    }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function adaptForce() {
    return apiPost(API.platforms, { club_id: state.club, force: true }, "强制适配")
      .then(function (r) {
        setStatus("已按人工决定出稿（这个动作记进了审核记录）");
        return r;
      }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function loadVersions(keepDraft) {
    return apiGet(API.versions, { club_id: state.club }, "稿件版本").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      if (!keepDraft) {
        var restore = (r.compose && (r.compose.segments || []).length) ? r.compose : r.current;
        if (restore && (restore.segments || []).length) {
          ensureDoc().fromDraft(restore);
          renderBoard();
          renderMaterials();
          refreshPreview();
        }
      }
      return r;
    });
  }

  function rollbackDraft() {
    return apiGet(API.versions, { club_id: state.club }, "稿件版本").then(function (r) {
      var list = (r.versions || []).filter(function (v) { return v.seq != null; });
      if (!list.length) { setStatus("没有可回滚的版本", "err"); return null; }
      return doRollback(list[0].seq, "稿件");
    });
  }

  function rollbackMaterialGraph() { return rollbackDraft(); }

  function doRollback(seq, label) {
    setStatus("正在回滚到 seq " + seq + "…");
    return apiPost(API.rollback, { club_id: state.club, to_seq: seq, reason: "workbench" },
      "回滚").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      state.preview = null;
      state.verify = null;
      toast(label + "已回滚到 seq " + seq + "（稿件与平台版一起回退）");
      return loadState().then(function () { return loadVersions(false); });
    }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function scanTopics() {
    return apiGet(API.topics, { club_id: state.club, scan: 1 }, "选题扫描").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      renderTopics(r.topics || [], r.added || {});
      return r;
    }).catch(function (e) { setStatus(e.message, "err"); });
  }
  function loadTopics() {
    return apiGet(API.topics, { club_id: state.club, scan: 0 }, "选题库").then(function (r) {
      if (r.error) { setStatus(r.error, "err"); return r; }
      renderTopics(r.topics || [], { total: r.count });
      return r;
    }).catch(function (e) { setStatus(e.message, "err"); });
  }
  function renderTopics(topics, added) {
    var box = $("wb-topic-list");
    if (!box) return;
    if ($("wb-topic-hint")) {
      $("wb-topic-hint").textContent = "共 " + topics.length + " 条" +
        (added && added.total != null ? "（库内 " + added.total + " 条）" : "");
    }
    box.innerHTML = topics.length ? topics.map(function (t) {
      return '<div class="wb-topic"><div class="wb-topic-top">' +
        '<span class="wb-gap-tag g-' + esc(t.kind || "no_source") + '">' +
        esc(t.label || t.kind) + "</span>" +
        '<span class="wb-topic-subject">' + esc(t.subject || "") + "</span></div>" +
        '<div class="wb-topic-why">' + esc(t.reason || "") + "</div></div>";
    }).join("") : '<div class="wb-empty">没有选题。先灌语料或做缺口检测。</div>';
  }
  function openTopics() {
    if ($("wb-topics")) $("wb-topics").classList.remove("hidden");
    return loadTopics();
  }
  function closeTopics() { if ($("wb-topics")) $("wb-topics").classList.add("hidden"); }

  function openDashboard() {
    if ($("wb-dash")) $("wb-dash").classList.remove("hidden");
    return apiGet(API.dashboard, { club_id: state.club }, "仪表盘").then(function (d) {
      if (d.error || !$("wb-dash-body")) return d;
      var gaps = d.gaps || {};
      $("wb-dash-body").innerHTML =
        '<div class="wb-kpi">' + kpi("素材", d.materials) + kpi("溯源边", d.cites) +
        kpi("缺口", (gaps.total || 0) + "（阻断 " + (gaps.blocking || 0) + "）") +
        kpi("选题", d.topics) +
        kpi("LLM 成功调用", (d.llm && d.llm.successful) || 0) +
        kpi("被闸门拦下", (d.llm && d.llm.denied) || 0) + "</div>" +
        '<div class="wb-div"></div><div class="wb-hint">' + esc(d.human_division || "") + "</div>" +
        '<div class="wb-hint">回滚次数：' + esc(String(d.rollbacks || 0)) +
        " · 素材图 seq：" + esc(String(d.head_seq)) + "</div>";
      return d;
    }).catch(function (e) { setStatus(e.message, "err"); });
  }
  function kpi(label, val) {
    return '<div class="wb-kpi-cell"><div class="wb-kpi-label">' + esc(label) +
      '</div><div class="wb-kpi-val">' + esc(String(val == null ? "-" : val)) + "</div></div>";
  }
  function closeDashboard() { if ($("wb-dash")) $("wb-dash").classList.add("hidden"); }

  function openReview() {
    if ($("wb-review")) $("wb-review").classList.remove("hidden");
    return apiGet(API.record, { club_id: state.club }, "审核记录").then(function (r) {
      if (r.error || !$("wb-review-body")) return r;
      var rec = r.record || {};
      $("wb-review-body").innerHTML =
        '<div class="wb-hint">生成于 ' + esc(rec.generated_at || "") + " · 素材图 seq " +
        esc(String(rec.head_seq)) + "</div>" +
        '<div class="wb-div"></div><div class="wb-plat-title">素材清单（带来源）</div>' +
        '<div class="wb-rows">' + (rec.materials || []).map(function (m) {
          return '<div class="wb-row"><span class="wb-row-step">' + esc(m.subject) +
            '</span><span class="wb-row-why">' + esc(TYPE_LABEL[m.type] || m.type) + " · " +
            esc(SOURCE_LABEL[m.source] || m.source) +
            (m.sensitive ? " · 含隐私" : "") + "</span></div>";
        }).join("") + "</div>" +
        '<div class="wb-div"></div><div class="wb-plat-title">缺口与处置（' +
        (rec.gaps || []).length + " 条）</div>" +
        '<div class="wb-rows">' + (rec.gaps || []).slice(0, 24).map(function (g) {
          return '<div class="wb-row"><span class="wb-row-step">' + esc(g.label) +
            '</span><span class="wb-row-why">' + esc(g.assertion) + " ← " + esc(g.source_eid) +
            (g.blocking ? " · 阻断" : "") + "</span></div>";
        }).join("") + "</div>" +
        '<div class="wb-div"></div><div class="wb-plat-title">操作留痕</div>' +
        '<div class="wb-rows">' + (rec.report_rows || []).slice(-14).reverse().map(function (x) {
          return '<div class="wb-row"><span class="wb-row-ts">' +
            esc((x.ts || "").slice(5, 16)) + '</span><span class="wb-row-step">' +
            esc(ACTION_CN[x.action] || x.action || x.step || "-") + "</span></div>";
        }).join("") + "</div>" +
        '<div class="wb-div"></div><div class="wb-hint">' + esc(rec.disclaimer || "") + "</div>";
      return r;
    }).catch(function (e) { setStatus(e.message, "err"); });
  }
  function closeReview() { if ($("wb-review")) $("wb-review").classList.add("hidden"); }

  function download(path, filename) {
    var qs = new URLSearchParams({ club_id: state.club }).toString();
    setStatus("正在导出…");
    return fetch(path + "?" + qs, { headers: { "X-Auth-Token": authToken() } })
      .then(function (res) {
        if (!res.ok) throw new Error("导出失败 (" + res.status + ")");
        return res.text();
      }).then(function (text) {
        var blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        setStatus("已导出 " + filename, "ok");
      }).catch(function (e) { setStatus(e.message, "err"); });
  }
  function exportReview() { return download(API.reviewExport, "审核记录_" + state.club + ".md"); }
  function exportPackage() { return download(API.packageExport, "宣发包_" + state.club + ".md"); }

  function showAllLinks() {
    var n = window.WBLinks ? window.WBLinks.showAll() : 0;
    setStatus(n ? "已画出 " + n + " 条溯源连线（滚动即清除）" : "这一稿还没有带来源的块");
    return n;
  }

  /* ==================== 设置：账号 / API / 外观 ==================== */
  function openSettings() {
    if ($("wb-set")) $("wb-set").classList.remove("hidden");
    renderSettingsUser();
    renderThemeRow();
    return loadLlmConfig();
  }
  function closeSettings() { if ($("wb-set")) $("wb-set").classList.add("hidden"); }
  function closeSettingsMask(ev) {
    if (ev && ev.target && ev.target.id !== "wb-set") return;
    closeSettings();
  }

  function renderSettingsUser() {
    var box = $("wb-set-user");
    var guest = $("wb-set-guest");
    var chip = $("wb-user-chip");
    if (state.user) {
      if (box) {
        box.innerHTML = '<div class="wb-set-row"><span class="wb-badge-plain">已登录</span>' +
          '<strong>' + esc(state.user) + "</strong>" +
          '<button class="wb-btn wb-btn-quiet" data-act="logout">退出登录</button></div>';
        var btn = box.querySelector('[data-act="logout"]');
        if (btn) btn.addEventListener("click", doLogout);
      }
      if (guest) guest.classList.add("hidden");
      if (chip) chip.textContent = state.user;
    } else {
      if (box) box.innerHTML = '<div class="wb-hint">还没登录。不登录也能用全部功能，' +
        "登录只是让 API Key 记在你名下。</div>";
      if (guest) guest.classList.remove("hidden");
      if (chip) chip.textContent = "设置";
    }
  }

  function setSetStatus(text, ok) {
    var el = $("wb-set-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "wb-hint" + (ok ? " wb-good" : ok === false ? " wb-bad" : "");
  }

  function afterLogin(r) {
    state.userToken = r.token || "";
    state.user = r.username || "";
    try { localStorage.setItem(USER_KEY, state.userToken); } catch (e) { /* 忽略 */ }
    renderSettingsUser();
    setSetStatus("已登录：" + state.user, true);
    return loadLlmConfig();
  }

  function doLogin() {
    return apiPost(API.authLogin, {
      username: ($("wb-set-name") || {}).value || "",
      password: ($("wb-set-pass") || {}).value || ""
    }, "登录").then(function (r) {
      if (!r.ok) { setSetStatus(r.error || "登录失败", false); return r; }
      return afterLogin(r);
    }).catch(function (e) { setSetStatus(e.message, false); });
  }

  function doRegister() {
    return apiPost(API.authRegister, {
      username: ($("wb-set-name") || {}).value || "",
      password: ($("wb-set-pass") || {}).value || ""
    }, "注册").then(function (r) {
      if (!r.ok) { setSetStatus(r.error || "注册失败", false); return r; }
      return afterLogin(r);
    }).catch(function (e) { setSetStatus(e.message, false); });
  }

  function doLogout() {
    return apiPost(API.authLogout, {}, "退出").then(function () {
      state.user = "";
      state.userToken = "";
      try { localStorage.removeItem(USER_KEY); } catch (e) { /* 忽略 */ }
      renderSettingsUser();
      setSetStatus("已退出登录", true);
      return loadLlmConfig();
    }).catch(function (e) { setSetStatus(e.message, false); });
  }

  function loadLlmConfig() {
    return apiGet(API.userLlm, null, "API 配置").then(function (r) {
      if (r.error) return r;
      // 有没有 Key 决定六角色走哪条路线: 有 → 团队(API 多智能体); 没有 → 确定性
      state.llmReady = !!(r.configured || r.fallback_available);
      if ($("wb-llm-base")) $("wb-llm-base").value = r.api_base || "";
      if ($("wb-llm-model")) $("wb-llm-model").value = r.model_name || "";
      if ($("wb-llm-provider")) $("wb-llm-provider").value = r.provider || "openai";
      if ($("wb-llm-key")) $("wb-llm-key").value = "";
      if ($("wb-llm-status")) {
        $("wb-llm-status").textContent = r.configured
          ? "已配置 Key（" + (r.api_key_masked || "") + "）；大模型只用在润色/合规/多平台改写这几个授权点。"
          : "没配也能用：六角色建议与三渠道预览都是确定性实现，会自动降级。";
      }
      return r;
    }).catch(function (e) { setStatus(e.message, "err"); });
  }

  function saveLlm() {
    var body = {
      api_base: ($("wb-llm-base") || {}).value || "",
      model_name: ($("wb-llm-model") || {}).value || "",
      provider: ($("wb-llm-provider") || {}).value || ""
    };
    var key = ($("wb-llm-key") || {}).value || "";
    if (key) body.api_key = key;
    return apiPost(API.userLlm, body, "保存 API").then(function (r) {
      if (!r.ok) { setLlmStatus(r.error || "保存失败", false); return r; }
      setLlmStatus("已保存" + (key ? "（Key 已更新）" : ""), true);
      if ($("wb-llm-key")) $("wb-llm-key").value = "";
      return r;
    }).catch(function (e) { setLlmStatus(e.message, false); });
  }

  function setLlmStatus(text, ok) {
    var el = $("wb-llm-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "wb-hint" + (ok ? " wb-good" : ok === false ? " wb-bad" : "");
  }

  function testLlm() {
    setLlmStatus("正在测试连通…");
    return apiPost(API.userLlmTest, {}, "测试连通").then(function (r) {
      if (r.ok) setLlmStatus("通了：" + (r.reply || "") + "（模型 " + (r.model || "-") + "）", true);
      else setLlmStatus("没通：" + (r.error || "未知原因"), false);
      return r;
    }).catch(function (e) { setLlmStatus(e.message, false); });
  }

  function clearLlm() {
    return apiPost(API.userLlm, { clear: true }, "清除 API").then(function (r) {
      if (!r.ok) { setLlmStatus(r.error || "清除失败", false); return r; }
      setLlmStatus("已清除。确定性功能不受影响。", true);
      return loadLlmConfig();
    }).catch(function (e) { setLlmStatus(e.message, false); });
  }

  function renderThemeRow() {
    var box = $("wb-theme-row");
    if (!box) return;
    box.innerHTML = THEMES.map(function (t) {
      return '<button class="wb-btn' + (currentTheme() === t ? " wb-btn-primary" : "") +
        '" data-theme="' + t + '">' + esc(THEME_LABEL[t]) + "</button>";
    }).join("");
    box.querySelectorAll("[data-theme]").forEach(function (b) {
      b.addEventListener("click", function () {
        applyTheme(b.dataset.theme);
        renderThemeRow();
      });
    });
  }

  /* ==================== 命令面板 ==================== */
  var COMMANDS = [
    { id: "seed", label: "灌入演示语料", hint: "11 条素材：历史层 3 + 当年层 8", run: seedDemo },
    { id: "autodraft", label: "让系统先出一版", hint: "之后拖着改", run: autoDraft },
    { id: "verify", label: "核验这一稿", hint: "断言溯源；有阻断项不许发布", run: verify },
    { id: "checklist", label: "待核实清单", hint: "矛盾项并列两版，由你选", run: openChecklist },
    { id: "adapt", label: "多平台适配（发布）", hint: "订阅号 / 短视频 / 海报", run: adapt },
    { id: "force", label: "仍有阻断项也要出稿", hint: "记入审核记录", run: adaptForce },
    { id: "topics", label: "选题（缺口驱动的扫描）", run: openTopics },
    { id: "assist_det", label: "确定性复核这一稿（不调 API）",
      hint: "同一批角色，零 LLM，结论可复现", run: function () {
        var roles = selectedRoles();
        if (!roles.length) {
          setStatus("先在中栏下面勾一个角色", "err");
          return Promise.resolve();
        }
        resetAiLog();
        return runDeterministicAssist(roles, true);
      } },
    { id: "review", label: "采编复盘（六角色团队 · 走 API）",
      hint: "勾选的角色逐个发言，SSE 流式进右栏", run: startReviewPanel },
    { id: "hots", label: "让团队重核验一遍", run: function () { return runHots("re_verify"); } },
    { id: "close_rv", label: "结束采编复盘会话", run: closeReviewSession },
    { id: "dashboard", label: "仪表盘（确定性 vs LLM）", run: openDashboard },
    { id: "record", label: "审核记录（证据与留痕）", run: openReview },
    { id: "export_record", label: "导出《审核记录》Markdown", run: exportReview },
    { id: "export_package", label: "导出「宣发包」（三平台物料）", run: exportPackage },
    { id: "rollback", label: "回滚到上一个版本", run: rollbackDraft },
    { id: "links", label: "看依赖（画出溯源连线）", run: showAllLinks },
    { id: "clear", label: "取消选中", run: clearPick },
    { id: "theme", label: "切换主题（深色 / 浅色 / 纸质）", run: toggleTheme },
    { id: "settings", label: "设置：账号与 API 配置", run: openSettings }
  ];

  function renderPalette(filter) {
    var box = $("wb-palette-list");
    if (!box) return;
    var f = String(filter || "").trim();
    var items = COMMANDS.filter(function (c) {
      return !f || (c.label + " " + (c.hint || "") + " " + c.id).indexOf(f) >= 0;
    });
    box.innerHTML = items.length ? items.map(function (c) {
      return '<button class="wb-cmd" data-id="' + esc(c.id) + '">' +
        '<span class="wb-cmd-label">' + esc(c.label) + "</span>" +
        (c.hint ? '<span class="wb-cmd-hint">' + esc(c.hint) + "</span>" : "") + "</button>";
    }).join("") : '<div class="wb-empty">没有匹配的命令。</div>';
    box.querySelectorAll(".wb-cmd").forEach(function (b) {
      b.addEventListener("click", function () { runCommand(b.dataset.id); });
    });
  }

  function runCommand(id) {
    var c = COMMANDS.filter(function (x) { return x.id === id; })[0];
    if (!c) return;
    closePalette();
    c.run();
  }

  function openPalette() {
    if ($("wb-palette")) $("wb-palette").classList.remove("hidden");
    var inp = $("wb-palette-input");
    if (inp) { inp.value = ""; if (typeof inp.focus === "function") inp.focus(); }
    renderPalette("");
  }
  function closePalette() { if ($("wb-palette")) $("wb-palette").classList.add("hidden"); }
  function closePaletteMask(ev) {
    if (ev && ev.target && ev.target.id !== "wb-palette") return;
    closePalette();
  }

  /* ==================== 采编复盘（真团队 · SSE，写进右栏） ==================== */
  function rvSay(member, text, kind) {
    return pushAi({ role: member, label: state.review.label[member] || member || "团队",
                    kind: kind || "team", text: text });
  }
  function rvSys(text) { return pushAi({ sys: true, text: text }); }

  /** 团队发来任何东西都算"还活着"(进度事件): 只重置静默计时。 */
  function noteTeamProgress() {
    state.review.progress = (state.review.progress || 0) + 1;
    armTeamWatchdog();
  }

  /** 真有角色给出意见: 既算活着, 也算"团队确实有产出"。 */
  function noteExpertAnswer() {
    state.review.answered = (state.review.answered || 0) + 1;
    noteTeamProgress();
  }

  function rvLoadExperts() {
    if (Object.keys(state.review.label).length) return Promise.resolve();
    return apiGet(API.reviewExperts, null, "角色目录").then(function (r) {
      (r.experts || []).forEach(function (e) { state.review.label[e.id] = e.label; });
    }).catch(function () { /* 目录拿不到就显示 id */ });
  }

  function rvOpenStream() {
    var id = state.review.id;
    if (!id) return;
    if (state.review.es) state.review.es.close();
    state.review.es = new EventSource(API.reviewStream + "?review_id=" + encodeURIComponent(id) +
      "&token=" + encodeURIComponent(authToken()));
    state.review.es.onmessage = function (ev) {
      var c;
      try { c = JSON.parse(ev.data); } catch (e) { return; }
      var t = c.type, p = c.payload || {};
      if (t === "history") {
        (p.messages || []).forEach(function (m) { rvSay(m.member || "review_host", m.text); });
      } else if (t === "answer" && p.output) {
        noteExpertAnswer();
        rvSay(c.source_member || "review_host", p.output);
      } else if (t === "team_message") {
        noteExpertAnswer();
        rvSay(p.sender || "review_host", p.body);
      } else if (t === "message" && p.event_type === "review_dispatch_started") {
        noteTeamProgress();
        rvSys("主持正在召集专家（共 " + (p.count || "?") + " 位）。" +
          "每个专家都要先读素材库/待核实清单/稿件才开口，请给它几十秒…");
        if ($("wb-ai-state")) $("wb-ai-state").textContent = "团队(API) 专家发言中";
      } else if (t === "message" && p.event_type === "review_dispatch_done") {
        if (!p.count) { teamMaybeFallback("专家一个都没产出报告"); return; }
        noteTeamProgress();
        rvSys("各角色意见已齐，主持在汇总");
      } else if (t === "message" && p.event_type === "review_dispatch_failed") {
        teamMaybeFallback("有个角色没拿到意见（" + (p.reason || "原因未知") + "）");
      } else if (t === "message" && p.event_type === "team.runtime_ready") {
        noteTeamProgress();
        rvSys("团队已就绪（名册注册完成），开始逐个点名专家");
      } else if (t === "hots_result") {
        noteTeamProgress();
        rvRenderHots(p);
      } else if (t === "error") {
        teamMaybeFallback("团队报错：" + (p.error || ""));
      } else if (t === "done") {
        rvSys("复盘已结束");
        state.review.id = null;
        state.review.active = false;
        if (state.review.watchdog) { clearTimeout(state.review.watchdog); state.review.watchdog = null; }
        if ($("wb-ai-state") && state.engine === "team") $("wb-ai-state").textContent = "团队 已结束";
        if (state.review.es) { state.review.es.close(); state.review.es = null; }
      }
    };
    state.review.es.onerror = function () { rvSys("SSE 连接中断（会话刚结束时属正常）"); };
  }

  function rvRenderHots(p) {
    if (!p.ok) { rvSys("团队重核验失败：" + (p.error || "")); return; }
    var r = p.result || {};
    if (r.verify && r.verify.summary) rvSay("fact_checker", r.verify.summary);
    if (r.compliance && r.compliance.summary) rvSay("compliance_reviewer", r.compliance.summary);
    (r.compliance && r.compliance.risks || []).forEach(function (x) {
      rvSay("compliance_reviewer", "【" + (x.kind || "风险") + "】" + (x.quote || "") +
        "\n建议：" + (x.advice || ""));
    });
    (r.need_verify || []).length && rvSys("需要你去核实：" + r.need_verify.join("；"));
  }

  /**
   * 命令面板里的「采编复盘」= 强制走团队路线(即使这次会话还没确认 Key)。
   * 和「启动智能辅助」是同一条代码路径, 只是跳过了 llmReady 判断 —— 方便配置好之后
   * 直接从命令面板重试。
   */
  function startReviewPanel() {
    var roles = selectedRoles();
    if (!roles.length) roles = state.roles.map(function (r) { return r.id; });
    if (ensureDoc().isEmpty()) {
      rvSys("中间还是空的：先把左边素材拖进来，团队才有东西看。");
      return Promise.resolve();
    }
    return rvLoadExperts().then(function () { return startTeamAssist(roles); });
  }

  function closeReviewPanel() { return closeReviewSession(); }

  function runHots(command) {
    if (!state.review.id) { rvSys("先开一场采编复盘"); return Promise.resolve(); }
    return apiPost(API.reviewHots, { review_id: state.review.id, command: command }, "团队指令")
      .then(function (r) {
        if (r.error) rvSys("未启动：" + r.error);
        else rvSys("已下达指令：" + command);
      }).catch(function (e) { rvSys("失败：" + e.message); });
  }

  function closeReviewSession() {
    if (!state.review.id) { rvSys("当前没有进行中的团队会话"); return Promise.resolve(); }
    return apiPost(API.reviewClose, { review_id: state.review.id }, "结束会话")
      .catch(function () { return {}; })
      .then(function () {
        if (state.review.es) { state.review.es.close(); state.review.es = null; }
        rvSys("会话已结束");
        state.review.id = null;
        state.review.active = false;
        if ($("wb-ai-state")) $("wb-ai-state").textContent = "已结束";
      });
  }

  /* ==================== 主题 ==================== */
  var THEME_KEY = "wb_theme";
  var THEMES = ["dark", "light", "paper"];
  var THEME_LABEL = { dark: "深色", light: "浅色", paper: "纸质" };

  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme") || "dark";
    return THEMES.indexOf(t) >= 0 ? t : "dark";
  }
  function applyTheme(name) {
    var t = THEMES.indexOf(name) >= 0 ? name : "dark";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* 忽略 */ }
    var btn = $("wb-theme-btn");
    if (btn) btn.textContent = THEME_LABEL[t];
    return t;
  }
  function toggleTheme() {
    var next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    applyTheme(next);
    renderThemeRow();
    setStatus("已切换到「" + THEME_LABEL[next] + "」主题");
    return next;
  }
  function restoreTheme() {
    var t = "dark";
    try { t = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) { /* 忽略 */ }
    applyTheme(t);
  }

  /* ==================== 键盘与拖拽绑定 ==================== */
  function bindBoardDrag() {
    var wrap = $("wb-boardwrap");
    if (!wrap) return;
    document.addEventListener("pointerdown", function (ev) {
      var t = ev.target;
      var psent = t.closest && t.closest(".wb-psent");
      if (psent) {
        var eid = psent.dataset.eid;
        var si = parseInt(psent.dataset.si, 10);
        var m = state.matById[eid] || {};
        var text = Doc.splitSentences(m.content)[si] || "";
        if (!text) return;
        ev.preventDefault();
        psent.classList.add("dragging");
        window.addEventListener("pointerup", function () {
          psent.classList.remove("dragging");
        }, { once: true });
        startDrag(ev, {
          from: "pool", text: text,
          block: { text: text, eid: eid, si: si, band: (m.badge && m.badge.band) || "",
                   source: m.source, authority: m.authority, from: "material" }
        });
        return;
      }
      var head = t.closest && t.closest(".wb-mat-head");
      if (head) {
        var card = head.closest(".wb-mat");
        var eid2 = card.dataset.eid;
        var m2 = state.matById[eid2] || {};
        var sents = Doc.splitSentences(m2.content);
        if (!sents.length) return;
        ev.preventDefault();
        card.classList.add("dragging");
        window.addEventListener("pointerup", function () {
          card.classList.remove("dragging");
        }, { once: true });
        startDrag(ev, {
          from: "pool", text: sents.join("。"),
          block: { text: sents.join("，"), eid: eid2, whole: true,
                   band: (m2.badge && m2.badge.band) || "", source: m2.source,
                   authority: m2.authority, from: "material" }
        });
        return;
      }
      var brick = t.closest && t.closest(".wb-brick");
      if (brick && !(t.closest && (t.closest(".wb-brick-x") || t.closest(".wb-brick-ref")))) {
        var d = ensureDoc();
        var b = d.find(brick.dataset.uid);
        if (!b) return;
        ev.preventDefault();
        brick.classList.add("dragging");
        window.addEventListener("pointerup", function () {
          brick.classList.remove("dragging");
        }, { once: true });
        startDrag(ev, { from: "doc", uid: b.uid, text: b.text });
      }
    });
  }

  function bindKeyboard() {
    document.addEventListener("keydown", function (ev) {
      var t = ev.target || {};
      var tag = (t.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable) {
        if (ev.key === "Escape" && t.blur) t.blur();
        return;
      }
      if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        openPalette();
        return;
      }
      if (ev.key === "Escape") {
        if ($("wb-palette") && !$("wb-palette").classList.contains("hidden")) { closePalette(); return; }
        if ($("wb-set") && !$("wb-set").classList.contains("hidden")) { closeSettings(); return; }
        clearPick();
        return;
      }
      if (!state.selected) return;
      var d = ensureDoc();
      var i = d.indexOf(state.selected.uid);
      if (i < 0) return;
      if (ev.altKey && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) {
        ev.preventDefault();
        d.move(state.selected.uid, { delta: ev.key === "ArrowUp" ? -1 : 1 });
        renderBoard();
        setStatus("已用键盘挪动这一块");
        return;
      }
      if (ev.key === "Backspace" || ev.key === "Delete") {
        ev.preventDefault();
        takeOut(state.selected.uid);
        state.selected = null;
      }
    });
  }

  /* ==================== 测试播种口 ==================== */
  function seedForTest(fixtures) {
    var f = fixtures || {};
    var st = f.state || {};
    state.materials = st.materials || [];
    state.matById = {};
    state.materials.forEach(function (m) { state.matById[m.eid] = m; });
    if (st.head_seq != null && $("wb-seq-chip")) $("wb-seq-chip").textContent = "seq " + st.head_seq;
    state.checklist = (f.checklist && f.checklist.check_list) || [];
    var d = ensureDoc();
    var draft = (f.versions && f.versions.compose) || (f.versions && f.versions.current) ||
      (f.draft && f.draft.draft) || null;
    if (draft) d.fromDraft(draft);
    state.preview = f.preview || null;
    bindBoardDrag();
    bindKeyboard();
    renderMaterials();
    renderBoard();
    renderPreview();
    renderRoles();
    renderChecklist((f.checklist && f.checklist.summary) || {});
    return state;
  }

  /** 拖拽测试口: 走"落点解析 → 插/挪/收回"这条真实路径，绕开指针事件管线。 */
  function dropForTest(payload, x, y) {
    return applyDrop(payload, resolveDrop(x, y));
  }

  /** 把素材从池子里直接拖进稿子(等价于一次完整拖拽), 供测试与演示脚本用。 */
  function dragMaterialForTest(eid, whole, index) {
    var m = state.matById[eid] || {};
    var sents = Doc.splitSentences(m.content);
    if (!sents.length) return null;
    var block = whole
      ? { text: sents.join("，"), eid: eid, whole: true, band: (m.badge && m.badge.band) || "",
          source: m.source, authority: m.authority, from: "material" }
      : { text: sents[0], eid: eid, si: 0, band: (m.badge && m.badge.band) || "",
          source: m.source, authority: m.authority, from: "material" };
    return applyDrop({ from: "pool", text: block.text, block: block }, { index: index == null ? 9999 : index });
  }

  function setRolesForTest(ids) {
    state.sel = {};
    (ids || []).forEach(function (id) { state.sel[id] = true; });
    renderRoles();
    return selectedRoles();
  }

  function applyAiActionForTest(msg) {
    applyAiAction(msg, (msg && msg._btn) || null);
    return ensureDoc().text();
  }

  /* ==================== 启动 ==================== */
  /* ==================== 截图/演示场景（?scene=…，只调已有函数） ====================
   * 用途只有一个: 让「实机截图」可复现 —— tools/capture_shots.py 用 Edge 无头模式
   * 打开 ?scene=xxx 就能截到某一个具体界面状态, 不必手工点。
   * 它不注入任何假数据: 每个场景都只是调用界面上本来就有的动作。
   */
  function sceneParam(name) {
    try {
      return new URLSearchParams(window.location.search || "").get(name) || "";
    } catch (e) { return ""; }
  }

  function runScene(name) {
    if (!name) return Promise.resolve();
    // 数据由 tools/capture_shots.py 事先通过 API 灌好(各场景一个独立工作区),
    // 这里只负责把某个界面状态摆出来 —— 不在截图时写库, 免得场景之间互相污染。
    return Promise.resolve().then(function () {
      if (name === "stack") return null;                       // 素材池 + 积木流 + 预览
      if (name === "assist") {                                 // 六角色辅助跑完停在那
        // 先把一条含隐私的素材拖进来: 这样合规角色会给出「脱敏」建议,
        // 截图上才看得到「应用」按钮(否则演示语料里没有可应用的改动)。
        var sens = state.materials.filter(isSensitive)[0];
        if (sens) dragMaterialForTest(sens.eid, true);
        setRolesForTest(["fact_checker", "compliance_reviewer", "platform_operator"]);
        return state.llmReady ? startTeamAssist(selectedRoles())
          : runDeterministicAssist(selectedRoles(), true);
      }
      if (name === "palette") { openPalette(); return null; }
      if (name === "checklist") return openChecklist();
      if (name === "settings") return openSettings();
      if (name === "dashboard") return openDashboard();
      if (name === "record") return openReview();
      if (name === "topics") return openTopics();
      return null;
    });
  }

  function init() {
    state.doc = Doc.createDoc({ onChange: boardHook });
    restoreTheme();
    var clubParam = sceneParam("club");
    if (clubParam) state.club = clubParam;                     // 截图用独立工作区, 不脏演示数据
    var themeParam = sceneParam("theme");
    if (themeParam) applyTheme(themeParam);
    bindKeyboard();
    bindBoardDrag();
    renderRoles();
    renderBoard();
    renderPreview();
    var intake = $("wb-in-text");
    if (intake) {
      intake.addEventListener("input", onIntakeInput);
      intake.addEventListener("paste", function () { setTimeout(onIntakeInput, 30); });
      intake.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
          ev.preventDefault();
          analyze().then(ingest);
        }
      });
    }
    var ask = $("wb-ai-input");
    if (ask) {
      ask.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); askAi(); }
      });
    }
    Promise.resolve()
      .then(function () { return apiGet(API.authMe, null, "账号"); })
      .then(function (r) {
        if (r && r.ok) { state.user = r.username || ""; renderSettingsUser(); }
      })
      .catch(function () { /* 未登录是常态 */ })
      .then(function () { return loadRoles(); })
      .then(function () { return loadLlmConfig(); })
      .then(function () { return loadState(); })
      .then(function () { return loadVersions(false); })
      .then(function () { return loadChecklist(); })
      .then(function () { return runScene(sceneParam("scene")); })
      .catch(function () { /* 各步自己已报状态 */ });
  }

  window.WBS = {
    state: state, API: API, init: init,
    seedForTest: seedForTest, dropForTest: dropForTest,
    dragMaterialForTest: dragMaterialForTest,
    setRolesForTest: setRolesForTest,
    applyAiActionForTest: applyAiActionForTest,
    // 素材
    seedDemo: seedDemo, loadState: loadState, analyze: analyze, ingest: ingest,
    // 稿件板
    autoDraft: autoDraft, clearPick: clearPick, showAllLinks: showAllLinks,
    takeOut: takeOut, pickBrick: pickBrick,
    // 六步
    verify: verify, adapt: adapt, adaptForce: adaptForce,
    openChecklist: openChecklist, closeChecklist: closeChecklist,
    loadChecklist: loadChecklist, resolveConflict: resolveConflict,
    // 角色与辅助
    loadRoles: loadRoles, toggleRole: toggleRole, startAssist: startAssist, askAi: askAi,
    // 设置
    openSettings: openSettings, closeSettings: closeSettings,
    closeSettingsMask: closeSettingsMask,
    doLogin: doLogin, doRegister: doRegister, doLogout: doLogout,
    saveLlm: saveLlm, testLlm: testLlm, clearLlm: clearLlm,
    loadLlmConfig: loadLlmConfig,
    // 选题 / 仪表盘 / 审核记录
    openTopics: openTopics, closeTopics: closeTopics,
    scanTopics: scanTopics, loadTopics: loadTopics,
    openDashboard: openDashboard, closeDashboard: closeDashboard,
    openReview: openReview, closeReview: closeReview,
    exportReview: exportReview, exportPackage: exportPackage,
    // 回滚
    rollbackDraft: rollbackDraft, rollbackMaterialGraph: rollbackMaterialGraph,
    // 命令面板 / 主题
    openPalette: openPalette, closePalette: closePalette,
    closePaletteMask: closePaletteMask, runCommand: runCommand,
    toggleTheme: toggleTheme, applyTheme: applyTheme,
    // 采编复盘(团队)
    startReviewPanel: startReviewPanel, closeReviewPanel: closeReviewPanel,
    runHots: runHots, closeReviewSession: closeReviewSession,
    // 预览
    refreshPreview: refreshPreview, renderPreview: renderPreview,
    renderPlatforms: renderPlatforms, setPlatform: setPlatform,
    highlightPlatform: highlightPlatform
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
