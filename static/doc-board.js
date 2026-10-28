/* ============================================================================
 * doc-board.js — 稿件板：单列积木流（先到在上）
 *
 * 设计决定（v3，来自用户明确要求）：
 *   - **不预设段落结构**。左栏拖过来的东西按到达顺序从上往下排成一条自由流，
 *     先到的在上、后到的在下；想换位置就拖，想拿掉就删。
 *   - 一个"块"= 一条素材（或素材里的一个分句）。块自带 eid，
 *     所以"这句话来自哪条素材"是组装的自然属性，不是额外步骤。
 *   - 板子只负责顺序与增删；核验、渲染、预览都在上层做。
 *
 * 与后端的关系：toSegments() 把积木流压成 segments（供 /verify 与 /platforms 用）。
 * 一块 = 一段，seg_id 由下标决定，所以核验结果能按位置对回具体的块。
 *
 * 无障碍兜底（拖拽不是唯一路径）：
 *   块上 Alt+↑/↓ 上下挪；Backspace/Delete 删除（素材回左栏）；
 *   左栏素材上 Enter 追加到流末尾。
 * ========================================================================== */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 与后端 components/memory_service.split_sentences 同口径
  function splitSentences(text) {
    return String(text || "")
      .split(/[。！？!?\n]+/)
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return !!s; });
  }

  var _uid = 0;
  function uid(prefix) {
    _uid += 1;
    return (prefix || "b") + "_" + Date.now().toString(36) + "_" + _uid;
  }

  /**
   * 一个积木块。
   * from: "material"（从素材池拖入）| "system"（系统先出的那一版）
   * whole: true = 整条素材拖进来的；si = 只拖了第几个分句（素材池据此把用掉的部分收起来）
   */
  function makeBlock(opts) {
    var o = opts || {};
    return {
      uid: o.uid || uid("b"),
      text: o.text || "",
      eid: o.eid || "",
      si: o.si == null || o.si === "" ? null : o.si,
      whole: !!o.whole,
      band: o.band || "",
      source: o.source || "",
      authority: o.authority == null ? null : o.authority,
      from: o.from || "material",
      note: o.note || ""
    };
  }

  function createDoc(opts) {
    var o = opts || {};
    var doc = {
      topic: o.topic || "",
      blocks: [],
      seq: o.seq == null ? null : o.seq,
      dirty: false,
      onChange: o.onChange || null
    };

    function changed() {
      doc.dirty = true;
      if (doc.onChange) doc.onChange(doc);
    }

    doc.indexOf = function (uid_) {
      for (var i = 0; i < doc.blocks.length; i++) {
        if (doc.blocks[i].uid === uid_) return i;
      }
      return -1;
    };

    doc.find = function (uid_) {
      var i = doc.indexOf(uid_);
      return i < 0 ? null : doc.blocks[i];
    };

    /** 插到 index 位置（省略/越界 = 追加到末尾）。 */
    doc.insert = function (block, index) {
      var b = makeBlock(block);
      var i = (index == null || index < 0 || index > doc.blocks.length)
        ? doc.blocks.length : index;
      doc.blocks.splice(i, 0, b);
      changed();
      return b;
    };

    doc.insertMany = function (blocks, index) {
      var list = (blocks || []).map(makeBlock);
      if (!list.length) return [];
      var i = (index == null || index < 0 || index > doc.blocks.length)
        ? doc.blocks.length : index;
      doc.blocks.splice.apply(doc.blocks, [i, 0].concat(list));
      changed();
      return list;
    };

    doc.remove = function (uid_) {
      var i = doc.indexOf(uid_);
      if (i < 0) return null;
      var b = doc.blocks.splice(i, 1)[0];
      changed();
      return b;
    };

    doc.removeMany = function (uids) {
      var want = {}, out = [];
      (uids || []).forEach(function (u) { want[u] = 1; });
      doc.blocks = doc.blocks.filter(function (b) {
        if (want[b.uid]) { out.push(b); return false; }
        return true;
      });
      if (out.length) changed();
      return out;
    };

    doc.update = function (uid_, patch) {
      var b = doc.find(uid_);
      if (!b) return null;
      Object.keys(patch || {}).forEach(function (k) { b[k] = patch[k]; });
      changed();
      return b;
    };

    /** 移动：to 可以是数字 index，或 {delta:-1|+1}，或 {index:n}。 */
    doc.move = function (uid_, to) {
      var from = doc.indexOf(uid_);
      if (from < 0) return false;
      var target;
      if (typeof to === "number") {
        target = to;
      } else if (to && to.delta) {
        target = from + to.delta;
      } else if (to && to.index != null) {
        target = to.index;
      } else {
        target = doc.blocks.length;
      }
      var b = doc.blocks.splice(from, 1)[0];
      if (target < 0) target = 0;
      if (target > doc.blocks.length) target = doc.blocks.length;
      if (target === from) { doc.blocks.splice(from, 0, b); return false; }
      doc.blocks.splice(target, 0, b);
      changed();
      return true;
    };

    doc.clear = function () {
      doc.blocks = [];
      changed();
      return doc;
    };

    doc.replaceAll = function (blocks) {
      doc.blocks = (blocks || []).map(makeBlock);
      return doc;
    };

    doc.isEmpty = function () { return doc.blocks.length === 0; };

    doc.count = function () { return doc.blocks.length; };

    /** 正文：按块顺序拼接，句末补句号（与后端切句口径一致）。 */
    doc.text = function () {
      return doc.blocks.map(function (b) {
        var t = b.text || "";
        return /[。！？!?]$/.test(t) ? t : t + "。";
      }).join("");
    };

    /** 用到的素材（去重，按首次出现）。 */
    doc.eids = function () {
      var seen = {}, out = [];
      doc.blocks.forEach(function (b) {
        if (b.eid && !seen[b.eid]) { seen[b.eid] = 1; out.push(b.eid); }
      });
      return out;
    };

    doc.sourcedCount = function () {
      return doc.blocks.filter(function (b) { return !!b.eid; }).length;
    };

    /**
     * 积木流 → 后端 segments。一块 = 一段，seg_id 稳定（blk_1..blk_n），
     * 核验结果据此按位置对回具体的块。
     */
    doc.toSegments = function () {
      return doc.blocks.map(function (b, i) {
        return {
          seg_id: "blk_" + (i + 1),
          kind: "block",
          label: "第 " + (i + 1) + " 块",
          text: b.text,
          source_refs: b.eid ? [b.eid] : []
        };
      });
    };

    /** 后端 segments（系统先出的那一版）→ 积木流。 */
    doc.fromSegments = function (segments) {
      var blocks = [];
      (segments || []).forEach(function (s) {
        var texts = splitSentences(s.text);
        if (!texts.length && s.text) texts = [String(s.text)];
        var refs = (s.source_refs || []).slice();
        texts.forEach(function (t, k) {
          blocks.push(makeBlock({
            text: t,
            // 系统稿只在段级知道来源，句级按顺序摊派
            eid: refs.length ? refs[Math.min(k, refs.length - 1)] : "",
            from: "system"
          }));
        });
      });
      doc.blocks = blocks;
      doc.dirty = false;
      return doc;
    };

    doc.fromDraft = function (draft) {
      doc.topic = (draft && draft.topic) || "";
      doc.seq = draft && draft.seq != null ? draft.seq : null;
      return doc.fromSegments((draft && draft.segments) || []);
    };

    /**
     * 把核验结果挂回块（按 seg_id = blk_<i+1> 对齐）。
     * 不改变块顺序，只追加状态字段，供渲染用。
     */
    doc.markVerify = function (verifyResult) {
      var bySeg = {};
      ((verifyResult && verifyResult.segments) || []).forEach(function (s) {
        bySeg[s.seg_id] = s;
      });
      doc.blocks.forEach(function (b, i) {
        var r = bySeg["blk_" + (i + 1)];
        b.status = r ? (r.block ? "blocked" : "verified") : "";
        b.gaps = (r && r.gaps) || [];
        b.claims = (r && r.claims) || [];
        b.cites = (r && r.cites) || [];
      });
      return doc;
    };

    return doc;
  }

  global.WBDoc = {
    createDoc: createDoc,
    makeBlock: makeBlock,
    splitSentences: splitSentences,
    esc: esc
  };
})(typeof window !== "undefined" ? window : globalThis);
