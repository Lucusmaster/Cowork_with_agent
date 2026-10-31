#!/usr/bin/env python3
"""多 Agent 协作台（网页）：http://127.0.0.1:8899

三个区：
  左：成员状态 + 任务列表
  中：消息流（可随时发新任务，排队执行）
  右：选中任务的流水线（计划 → 执行 → 验收 → 复核）与证据

  python bin/room_web.py [--port 8899]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .winproc import quiet_flags
from . import room  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
ROOM = ROOT / "room"
SESSION_STATE = ROOM / "session_state.json"

# 网页在线状态：页面每次轮询都会刷新 last_seen，关页面时尽力发一次 bye。
PRESENCE = {"last_seen": 0.0, "bye_at": 0.0, "ever": False}

AGENTS = {
    "deepseek": {"name": "DeepSeek", "role": "大脑 · 决策", "color": "#a371f7", "initial": "D"},
    "codex": {"name": "Codex", "role": "执行 · 写代码", "color": "#3fb950", "initial": "C"},
    "reviewer": {"name": "Codex 复核", "role": "复核 · 只读审查", "color": "#d29922", "initial": "R"},
    "relay": {"name": "人工转发", "role": "人接手", "color": "#f0883e", "initial": "人"},
}

STATUS_LABEL = {
    "planned": "已建卡",
    "executed": "已执行",
    "exec-failed": "执行失败",
    "failed": "验收失败",
    "passed": "通过",
    "stopped": "已停止",
}

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>多 Agent 协作台</title>
<style>
:root{
  --bg:#0a0d12; --panel:#0f131a; --panel2:#141922; --panel3:#181f29;
  --line:#1f2630; --line2:#2a3340;
  --fg:#dbe2ea; --dim:#7d8899; --dim2:#55606e;
  --brain:#a371f7; --exec:#3fb950; --review:#d29922; --user:#58a6ff; --sys:#64748b;
  --ok:#3fb950; --bad:#f85149; --warn:#d29922;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);overflow:hidden;
  font:13.5px/1.62 "Segoe UI","Microsoft YaHei",-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased}
.mono{font-family:"Cascadia Mono",Consolas,"SF Mono",monospace;font-size:12px}

/* ---------- header ---------- */
header{height:50px;display:flex;align-items:center;gap:14px;padding:0 16px;
  border-bottom:1px solid var(--line);background:linear-gradient(180deg,#111722,#0d1117)}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;font-size:13.5px;letter-spacing:.2px}
.brand .mark{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;
  background:linear-gradient(135deg,#a371f7,#58a6ff);color:#0a0d12;font-weight:800;font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 9px;border-radius:999px;
  border:1px solid var(--line2);background:var(--panel2);font-size:11.5px;color:var(--dim)}
.pill b{color:var(--fg);font-weight:600}
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim2)}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px #3fb95022;animation:pulse 1.6s infinite}
.dot.warn{background:var(--warn)}
@keyframes pulse{50%{opacity:.45}}
header .right{margin-left:auto;display:flex;align-items:center;gap:10px}

/* ---------- layout ---------- */
main{display:grid;grid-template-columns:250px minmax(0,1fr) 392px;height:calc(100vh - 50px)}
.col{min-width:0;min-height:0;display:flex;flex-direction:column}
.col.side{border-right:1px solid var(--line);background:var(--panel)}
.col.detail{border-left:1px solid var(--line);background:var(--panel)}
.colhead{height:34px;display:flex;align-items:center;gap:8px;padding:0 12px;
  font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim2);
  border-bottom:1px solid var(--line);flex:0 0 auto}
.colhead .count{margin-left:auto;color:var(--dim);letter-spacing:0}

/* ---------- members ---------- */
.members{padding:8px}
.member{display:flex;align-items:center;gap:9px;padding:7px 8px;border-radius:8px}
.member+.member{margin-top:2px}
.member:hover{background:var(--panel2)}
.mavatar{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;font-weight:700;
  font-size:11.5px;color:#0a0d12;flex:0 0 auto}
.mbody{min-width:0;flex:1}
.mname{font-size:12.5px;font-weight:600;display:flex;align-items:center;gap:6px}
.mrole{font-size:11px;color:var(--dim2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mstate{font-size:10.5px;color:var(--dim);display:flex;align-items:center;gap:5px;flex:0 0 auto}

/* ---------- task list ---------- */
.tasks{flex:1;overflow-y:auto;padding:6px 8px 14px}
.task{width:100%;text-align:left;background:transparent;border:1px solid transparent;border-radius:8px;
  padding:7px 9px;cursor:pointer;color:inherit;font:inherit;display:block}
.task:hover{background:var(--panel2)}
.task.sel{background:var(--panel3);border-color:var(--line2)}
.task .top{display:flex;align-items:center;gap:6px}
.task .tid{font-size:10.5px;color:var(--dim2);font-family:"Cascadia Mono",Consolas,monospace}
.task .title{font-size:12px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--fg)}
.chip{font-size:10px;padding:1px 6px;border-radius:999px;border:1px solid;line-height:1.5;flex:0 0 auto}
.chip.passed{color:var(--ok);border-color:#3fb95055;background:#3fb95014}
.chip.failed,.chip.exec-failed{color:var(--bad);border-color:#f8514955;background:#f8514914}
.chip.planned,.chip.executed{color:var(--user);border-color:#58a6ff55;background:#58a6ff14}
.chip.stopped{color:var(--dim);border-color:var(--line2);background:#ffffff08}

/* ---------- chat ---------- */
.stream{flex:1;overflow-y:auto;padding:14px 20px 6px;scroll-behavior:smooth}
.typingrow{display:flex;align-items:center;gap:8px;padding:6px 2px 10px;color:#8aa0b8;font-size:12.5px}
.typingrow .tdot{width:6px;height:6px;border-radius:50%;background:#a371f7;display:inline-block;
  animation:tblink 1.1s infinite ease-in-out}
.typingrow .tdot:nth-child(2){animation-delay:.18s}
.typingrow .tdot:nth-child(3){animation-delay:.36s}
@keyframes tblink{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-2px)}}
.row.pending{opacity:.65}
.row{display:flex;gap:10px;margin-bottom:11px;max-width:100%}
.row .av{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;font-weight:700;
  font-size:12px;color:#0a0d12;flex:0 0 auto;margin-top:1px}
.row .col-in{min-width:0;max-width:min(720px,86%)}
.meta{font-size:11px;color:var(--dim2);margin-bottom:3px;display:flex;align-items:center;gap:7px}
.meta .who{font-weight:600;color:var(--dim)}
.bubble{white-space:pre-wrap;word-break:break-word;background:var(--panel2);
  border:1px solid var(--line);border-left:2px solid var(--line2);border-radius:3px 9px 9px 3px;
  padding:9px 13px;font-size:13px}
.row.user .bubble{background:#101a29;border-color:#26456b;border-left-color:var(--user)}
.row.user{flex-direction:row-reverse}
.row.user .col-in{max-width:min(680px,84%)}
.row.user .av{background:var(--user)}
.row.user .meta{justify-content:flex-end}
.row.deepseek .bubble{border-left-color:var(--brain)}
.row.deepseek .av{background:var(--brain)}
.row.codex .bubble,.row.reviewer .bubble{border-left-color:var(--exec)}
.row.codex .av,.row.reviewer .av{background:var(--exec)}
.row.reviewer .bubble{border-left-color:var(--review)}
.row.reviewer .av{background:var(--review)}
.row.user .bubble{border-left-color:var(--user)}
.row.relay .bubble{border-left-color:#f0883e}

/* events: slim timeline */
.ev{display:flex;gap:9px;align-items:flex-start;margin:0 0 8px;padding-left:2px}
.ev .rail{width:28px;display:flex;justify-content:center;padding-top:5px;flex:0 0 auto}
.ev .rail i{width:7px;height:7px;border-radius:50%;background:var(--line2);display:block}
.ev.run .rail i{background:var(--exec);box-shadow:0 0 0 3px #3fb9501f;animation:pulse 1.6s infinite}
.ev.ok .rail i{background:var(--ok)}
.ev.bad .rail i{background:var(--bad)}
.ev .txt{font-size:12px;color:var(--dim);padding:3px 0;min-width:0}
.ev .txt b{color:var(--fg);font-weight:600}
.ev .txt .t{color:var(--dim2);font-size:10.5px;margin-left:6px;font-family:"Cascadia Mono",Consolas,monospace}

.sys{display:flex;align-items:center;gap:10px;color:var(--dim2);font-size:11.5px;margin:10px 0 12px}
.sys:before,.sys:after{content:"";height:1px;background:var(--line);flex:1}

/* ---------- composer ---------- */
.composer{flex:0 0 auto;border-top:1px solid var(--line);background:var(--panel);padding:10px 14px 12px}
.composer .box{display:flex;gap:9px;align-items:flex-end;background:var(--bg);
  border:1px solid var(--line2);border-radius:10px;padding:8px 8px 8px 12px;transition:border-color .15s}
.composer .box:focus-within{border-color:#3d5a7d}
#input{flex:1;resize:none;max-height:140px;min-height:22px;background:transparent;color:var(--fg);
  border:0;outline:none;font:inherit;padding:3px 0}
#send{width:30px;height:30px;border-radius:7px;border:0;background:var(--user);color:#06111f;
  cursor:pointer;display:grid;place-items:center;flex:0 0 auto}
#send:disabled{opacity:.35;cursor:default}
.hint{display:flex;gap:12px;color:var(--dim2);font-size:11px;padding:7px 2px 0}
.hint kbd{font-family:inherit;border:1px solid var(--line2);border-radius:4px;padding:0 4px;font-size:10px;color:var(--dim)}

/* ---------- detail ---------- */
.detail-scroll{flex:1;overflow-y:auto;padding:12px 13px 24px}
.dhead{padding-bottom:10px;border-bottom:1px solid var(--line);margin-bottom:11px}
.dhead .tid{font-family:"Cascadia Mono",Consolas,monospace;font-size:11px;color:var(--dim2)}
.dhead h2{font-size:14px;font-weight:650;margin:3px 0 7px;line-height:1.4}
.drow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}

/* pipeline */
.pipe{display:flex;align-items:center;gap:0;margin:12px 0 14px}
.step{flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;position:relative}
.step .bullet{width:19px;height:19px;border-radius:50%;border:1px solid var(--line2);background:var(--panel2);
  display:grid;place-items:center;font-size:9.5px;color:var(--dim2);z-index:1}
.step .lbl{font-size:10.5px;color:var(--dim2)}
.step.done .bullet{background:#3fb950;border-color:#3fb950;color:#0a0d12}
.step.done .lbl{color:var(--dim)}
.step.active .bullet{border-color:var(--exec);color:var(--exec);box-shadow:0 0 0 3px #3fb95018;animation:pulse 1.6s infinite}
.step.active .lbl{color:var(--exec)}
.step.bad .bullet{background:var(--bad);border-color:var(--bad);color:#0a0d12}
.step-link{height:1px;background:var(--line2);flex:0 0 10px;margin-top:-14px}

.sect{margin-bottom:14px}
.sect h3{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim2);
  margin-bottom:6px;display:flex;align-items:center;gap:6px}
.sect h3 .n{margin-left:auto;font-size:10px;color:var(--dim2);letter-spacing:0;text-transform:none}
.kv{display:grid;grid-template-columns:70px 1fr;gap:4px 8px;font-size:12px}
.kv .k{color:var(--dim2)}
.kv .v{color:var(--fg);min-width:0;overflow-wrap:anywhere}
ul.acc{list-style:none;display:flex;flex-direction:column;gap:4px}
ul.acc li{font-size:12px;color:var(--dim);padding-left:14px;position:relative}
ul.acc li:before{content:"✓";position:absolute;left:0;color:var(--exec);font-size:10px;top:1px}
pre.ev-block{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:8px 10px;
  font-family:"Cascadia Mono",Consolas,monospace;font-size:11px;line-height:1.55;color:#9fb0c3;
  max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.empty{color:var(--dim2);font-size:12px;text-align:center;padding:30px 10px;line-height:1.9}
.wsrow{display:flex;gap:6px;align-items:center;font-size:11ptx;color:var(--dim)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#232c38;border-radius:9px;border:2px solid var(--panel)}
::-webkit-scrollbar-thumb:hover{background:#2f3a49}
::-webkit-scrollbar-track{background:transparent}
</style></head>
<body>
<header>
  <div class="brand"><span class="mark">A</span>多 Agent 协作台</div>
  <span class="pill" id="pill-task"><span class="dot" id="dot-live"></span><span id="live-txt">空闲</span></span>
  <span class="pill">账本 <b id="stat-msg">0</b> 条 · 任务 <b id="stat-tasks">0</b> 个</span>
  <div class="right">
    <span class="pill mono" id="clock">--:--:--</span>
  </div>
</header>
<main>
  <section class="col side">
    <div class="colhead">成员</div>
    <div class="members" id="members"></div>
    <div class="colhead">任务<span class="count" id="task-count">0</span></div>
    <div class="tasks" id="tasks"></div>
  </section>

  <section class="col">
    <div class="colhead">协作流<span class="count" id="stream-count"></span></div>
    <div class="stream" id="stream"></div>
    <div class="composer">
      <div class="box">
        <textarea id="input" rows="1" placeholder="发任务，例如：给 tools/wordstat.py 加 --exclude PATTERN（随时可发，会排队执行）"></textarea>
        <button id="send" title="发送 (Enter)">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
               stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/></svg>
        </button>
      </div>
      <div class="hint">
        <span><kbd>Enter</kbd> 发送</span><span><kbd>Shift</kbd>+<kbd>Enter</kbd> 换行</span>
        <span>我干活时你也能继续发，下一条会排队</span>
        <span id="project-line" class="projectline">项目：未设置</span>
        <button id="continue" title="接着上一阶段往下做（自动带上阶段报告、失败项、复核意见）">继续上一阶段</button>
      </div>
    </div>
  </section>

  <section class="col detail">
    <div class="colhead">当前任务</div>
    <div class="detail-scroll" id="detail">
      <div class="empty">点左边任一任务查看流水线<br>或直接在中间发一条新任务</div>
    </div>
  </section>
</main>
<script>
const AGENTS = __AGENTS__;
const STATUS_LABEL = __STATUS_LABEL__;
let lastMsgId = 0, selTask = null, tasks = [], msgs = 0;
const stream = document.getElementById('stream');

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hhmmss = s => (s || '').slice(11, 19);
const colorOf = from => (AGENTS[from] || {}).color || '#64748b';
const initialOf = from => (AGENTS[from] || {}).initial || (from === 'user' ? '我' : '·');

/* ---------- members ---------- */
function renderMembers(state){
  const host = document.getElementById('members');
  const rows = [
    {key:'user', name:'你', role:'发任务 · 决策边界', color:'#58a6ff', initial:'我', state:'online'},
    {key:'deepseek', ...AGENTS.deepseek, state: state.agents.deepseek},
    {key:'codex', ...AGENTS.codex, state: state.agents.codex},
    {key:'reviewer', ...AGENTS.reviewer, state: state.agents.reviewer},
  ];
  host.innerHTML = rows.map(r => {
    const st = r.state || 'idle';
    const on = st === 'working';
    return `<div class="member">
      <div class="mavatar" style="background:${r.color}">${esc(r.initial)}</div>
      <div class="mbody">
        <div class="mname">${esc(r.name)}</div>
        <div class="mrole">${esc(r.role)}</div>
      </div>
      <div class="mstate"><span class="dot ${on ? 'on' : ''}"></span>${on ? '工作中' : (st === 'reviewing' ? '复核中' : '空闲')}</div>
    </div>`;
  }).join('');
}

/* ---------- tasks ---------- */
function renderTasks(list){
  const host = document.getElementById('tasks');
  document.getElementById('task-count').textContent = list.length;
  document.getElementById('stat-tasks').textContent = list.length;
  if(!list.length){ host.innerHTML = '<div class="empty">还没有任务</div>'; return; }
  host.innerHTML = list.map(t => `
    <button class="task ${t.id === selTask ? 'sel' : ''}" data-id="${esc(t.id)}">
      <div class="top">
        <span class="tid">${esc(t.id)}</span>
        <span class="chip ${esc(t.status)}">${esc(STATUS_LABEL[t.status] || t.status)}</span>
        <span class="tid" style="margin-left:auto">${t.rounds}轮</span>
      </div>
      <div class="title">${esc(t.title)}</div>
    </button>`).join('');
  host.querySelectorAll('.task').forEach(el => el.onclick = () => { selTask = el.dataset.id; renderTasks(tasks); load(); });
}

/* ---------- stream ---------- */
function renderMsg(m){
  const from = m.from || 'system';
  if(m.kind === 'event'){
    const meta = m.meta || {};
    const phase = meta.phase || '';
    const cls = (meta.state === 'run') ? 'run' : (meta.state === 'bad' ? 'bad' : (meta.state === 'ok' ? 'ok' : ''));
    const el = document.createElement('div');
    el.className = 'ev ' + cls;
    el.innerHTML = `<div class="rail"><i></i></div>
      <div class="txt"><b>${esc(meta.actorLabel || (AGENTS[meta.actor] || {}).name || '调度')}</b> ${m.text}
      <span class="t">${hhmmss(m.at)}</span></div>`;
    stream.appendChild(el);
    return;
  }
  if(from === 'system'){
    const el = document.createElement('div');
    el.className = 'sys';
    el.textContent = m.text;
    stream.appendChild(el);
    return;
  }
  const el = document.createElement('div');
  el.className = 'row ' + from;
  el.innerHTML = `<div class="av" style="background:${colorOf(from)}">${esc(initialOf(from))}</div>
    <div class="col-in">
      <div class="meta"><span class="who" style="color:${colorOf(from)}">${esc(m.name || from)}</span>
      <span>${hhmmss(m.at)}</span>${m.kind && m.kind !== 'msg' ? `<span class="t">${esc(m.kind)}</span>` : ''}</div>
      <div class="bubble"></div>
    </div>`;
  el.querySelector('.bubble').textContent = m.text;
  stream.appendChild(el);
}

/* ---------- detail panel ---------- */
function pipe(task){
  const rounds = task.rounds || [];
  const lastReview = (task.reviews || [])[task.reviews.length - 1];
  const failed = task.status === 'failed' || task.status === 'exec-failed' || task.status === 'stopped';
  const steps = [
    {lbl:'计划', done:true},
    {lbl:'执行', done:rounds.length > 0, active: task.status === 'planned' && false, bad: task.status === 'exec-failed'},
    {lbl:'验收', done: task.status === 'passed', active: task.status === 'executed', bad: task.status === 'failed'},
    {lbl:'复核', done: !!(lastReview && lastReview.verdict === 'PASS'), active: task.status === 'passed' && !lastReview,
     bad: !!(lastReview && lastReview.verdict === 'NEEDS_FIX')},
  ];
  return steps.map((s,i) => `${i ? '<span class="step-link"></span>' : ''}
    <div class="step ${s.done ? 'done' : ''} ${s.active ? 'active' : ''} ${s.bad ? 'bad' : ''}">
      <span class="bullet">${s.done ? '✓' : (s.bad ? '!' : i+1)}</span><span class="lbl">${s.lbl}</span>
    </div>`).join('');
}

function renderDetail(task){
  const host = document.getElementById('detail');
  if(!task){
    host.innerHTML = '<div class="empty">点左边任一任务查看流水线<br>或直接在中间发一条新任务</div>';
    return;
  }
  const rounds = task.rounds || [];
  const lastRound = rounds[rounds.length - 1];
  const acc = (task.acceptance || []).map(a => `<li>${esc(a)}</li>`).join('');
  const verify = task.verify_tail ? `<pre class="ev-block">${esc(task.verify_tail)}</pre>` : '<div class="empty" style="padding:14px">还没有验收记录</div>';
  const review = task.review_text ? `<pre class="ev-block">${esc(task.review_text)}</pre>` : '<div class="empty" style="padding:14px">还没有复核记录</div>';
  const diff = task.diff_stat ? `<pre class="ev-block">${esc(task.diff_stat)}</pre>` : '';
  host.innerHTML = `
    <div class="dhead">
      <div class="tid">${esc(task.id)}</div>
      <h2>${esc(task.title)}</h2>
      <div class="drow">
        <span class="chip ${esc(task.status)}">${esc(STATUS_LABEL[task.status] || task.status)}</span>
        <span class="pill" style="height:22px">${rounds.length} 轮</span>
        ${lastRound ? `<span class="pill mono" style="height:22px">${esc(lastRound.model || '')} · ${esc(lastRound.effort || '')}</span>` : ''}
      </div>
    </div>
    <div class="pipe">${pipe(task)}</div>
    <div class="sect"><h3>目标</h3><div style="font-size:12px;color:var(--dim)">${esc(task.goal)}</div></div>
    <div class="sect"><h3>验收标准<span class="n">${(task.acceptance||[]).length}</span></h3><ul class="acc">${acc}</ul></div>
    <div class="sect"><h3>工作区</h3>
      <div class="kv"><div class="k">产物</div><div class="v mono" style="font-size:11px">${esc(task.workspace || '')}</div>
      <div class="k">账本</div><div class="v mono" style="font-size:11px">tasks/${esc(task.id)}/</div></div>
    </div>
    ${lastRound ? `<div class="sect"><h3>最近一轮执行</h3>
      <div class="kv">
        <div class="k">退出码</div><div class="v">${esc(lastRound.exit)}</div>
        <div class="k">耗时</div><div class="v">${esc(lastRound.seconds)}s</div>
        <div class="k">用量</div><div class="v">${lastRound.tokens ? Number(lastRound.tokens).toLocaleString() + ' tokens' : '未取到'}</div>
        <div class="k">小结</div><div class="v">${esc(lastRound.summary || '')}</div>
      </div></div>` : ''}
    <div class="sect"><h3>验收证据</h3>${verify}</div>
    <div class="sect"><h3>复核结论</h3>${review}</div>
    ${diff ? `<div class="sect"><h3>改动</h3>${diff}</div>` : ''}
  `;
}

/* ---------- poll ---------- */
async function load(){
  try{
    const q = new URLSearchParams({after: lastMsgId});
    if(selTask) q.set('task', selTask);
    const r = await fetch('/api/state?' + q.toString());
    const state = await r.json();
    (state.messages || []).forEach(m => { renderMsg(m); lastMsgId = Math.max(lastMsgId, m.id); });
    if((state.messages || []).length) stream.scrollTop = stream.scrollHeight;
    msgs = state.total_messages;
    document.getElementById('stat-msg').textContent = msgs;
    paintProject(state);
    paintTyping(state);
    renderMembers(state);
    const prev = JSON.stringify(tasks.map(t => t.status + t.rounds));
    tasks = state.tasks || [];
    if(JSON.stringify(tasks.map(t => t.status + t.rounds)) !== prev || !document.querySelector('#tasks .task')){
      renderTasks(tasks);
    }
    if(!selTask && tasks.length){ selTask = tasks[0].id; load(); return; }
    renderDetail(state.task || null);
    const busy = ['dispatch','verify','review'].filter(k => state.agents[k] === 'working');
    const liveTxt = document.getElementById('live-txt');
    const dot = document.getElementById('dot-live');
    if(busy.length){ liveTxt.textContent = busy.map(b => ({dispatch:'执行中',verify:'验收中',review:'复核中'}[b])).join(' / '); dot.classList.add('on'); }
    else { liveTxt.textContent = '空闲'; dot.classList.remove('on'); }
  }catch(e){ /* 服务重启中，下一轮再试 */ }
}

document.getElementById('clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false});
setInterval(() => document.getElementById('clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false}), 1000);

const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(140, input.scrollHeight) + 'px';
  sendBtn.disabled = !input.value.trim();
});
sendBtn.disabled = true;
function showPending(text){
  const el = document.createElement('div');
  el.className = 'row user pending';
  el.innerHTML = `<div class="av" style="background:${colorOf('user')}">${esc(initialOf('user'))}</div>
    <div class="col-in"><div class="meta"><span class="who" style="color:${colorOf('user')}">我</span>
    <span>${hhmmss(new Date().toISOString())}</span><span class="t">发送中</span></div>
    <div class="bubble">${esc(text)}</div></div>`;
  stream.appendChild(el);
  stream.scrollTop = stream.scrollHeight;
}

async function send(){
  const text = input.value.trim();
  if(!text) return;
  sendBtn.disabled = true;
  input.value = ''; input.style.height = 'auto';
  showPending(text);
  paintTyping({typing:{who:'deepseek'}});   // 立刻显示"正在思考"，不用等服务器
  await fetch('/api/post', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({as:'user', text, kind:'task'})});
  await load();
  input.focus();
}
sendBtn.onclick = send;

const contBtn = document.getElementById('continue');
contBtn.onclick = async () => {
  contBtn.disabled = true;
  contBtn.textContent = '已排入…';
  await fetch('/api/post', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({as:'user', text:'继续', kind:'task'})});
  await load();
  setTimeout(() => { contBtn.disabled = false; contBtn.textContent = '继续上一阶段'; }, 1500);
};
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
});
let typingEl = null;
function paintTyping(state){
  const who = (state && state.typing && state.typing.who) || '';
  if(!who){
    if(typingEl && typingEl.parentNode) typingEl.remove();
    return;
  }
  if(!typingEl){
    typingEl = document.createElement('div');
    typingEl.className = 'typingrow';
    typingEl.innerHTML = '<span class="tdot"></span><span class="tdot"></span><span class="tdot"></span>'
      + '<span class="twho">DeepSeek 正在思考…</span>';
  }
  stream.appendChild(typingEl);          // 始终贴在最后
  stream.scrollTop = stream.scrollHeight;
}

function paintProject(state){
  const el = document.getElementById('project-line');
  if(!el) return;
  const p = state.project || {};
  if(p.workspace){
    el.textContent = '项目：' + (p.name || '') + '（' + p.workspace + '）';
    el.title = p.workspace;
    document.getElementById('input').placeholder =
      '直接跟它们说就行，比如：继续 / 把登录页做出来 / 测试挂了帮我修（项目已绑 ' + (p.name || p.workspace) + '）';
  } else {
    el.textContent = '项目：未设置（说一句"工作区是 E:\\某个仓库"就行）';
  }
}

setInterval(load, 1200);
load();
input.focus();

/* 页面开着就一直在（load 每 1.2 秒打一次 /api/state，服务端据此判断在线）。
   关页面/关标签页时尽力发一次告别；发不出去也没关系，服务端有超时兜底。 */
function sayBye(){
  try{
    const body = JSON.stringify({reason: 'page-unloaded'});
    if(navigator.sendBeacon){
      navigator.sendBeacon('/api/bye', new Blob([body], {type: 'application/json'}));
    } else {
      fetch('/api/bye', {method:'POST', body, headers:{'Content-Type':'application/json'}, keepalive: true});
    }
  }catch(e){}
}
window.addEventListener('pagehide', sayBye);
window.addEventListener('beforeunload', sayBye);
</script>
</body></html>"""


def _write_session_state(collab: str, reason: str = "") -> None:
    payload = {
        "collab": collab,
        "reason": reason,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(ROOT),
    }
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        SESSION_STATE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _stop_worker() -> int:
    """按锁里的 pid 停掉工作流引擎（跨平台：Windows 用 taskkill，其余用 SIGTERM）。"""
    try:
        info = json.loads((ROOM / "worker.lock").read_text(encoding="utf-8"))
        pid = int(info.get("pid") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    if pid <= 0:
        return 0
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="replace", creationflags=quiet_flags())
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        (ROOM / "worker.lock").unlink(missing_ok=True)
    except OSError:
        pass
    return pid


TERMINAL_STATUS = {"passed", "failed", "exec-failed", "stopped"}


def _active_tasks() -> list[str]:
    """还没到终态的任务。有活要干的时候不能收工，否则会打断执行者。"""
    active: list[str] = []
    if not TASKS.is_dir():
        return active
    for directory in TASKS.iterdir():
        card = directory / "task.json"
        if not card.is_file():
            continue
        try:
            task = json.loads(card.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (task.get("status") or "") not in TERMINAL_STATUS:
            active.append(directory.name)
    return active


def _shutdown(reason: str) -> None:
    pid = _stop_worker()
    _write_session_state("ended", reason)
    try:
        detail = f"，引擎已停（pid {pid}）" if pid else "，引擎本来没在跑"
        room.post("system", f"协作已结束：{reason}{detail}", "event",
                  {"phase": "collab", "state": "ok", "actor": "deepseek"})
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)
    os._exit(0)


def _guardian(idle_limit: float, grace: float) -> None:
    """没人看着就收工：关页面立刻走（有 grace 缓冲），崩溃/强杀靠超时兜底。"""
    while True:
        time.sleep(1.5)
        if not PRESENCE["ever"]:
            continue          # 从没人打开过页面，不擅自关
        now = time.time()
        idle = now - PRESENCE["last_seen"]
        bye = PRESENCE["bye_at"]
        trigger = ""
        if bye and (now - bye) >= grace and idle >= grace:
            trigger = "网页已关闭"
        elif idle >= idle_limit:
            trigger = "网页已关闭（长时间没有心跳）"
        if not trigger:
            continue

        pending = _active_tasks()
        if pending:
            if not PRESENCE.get("deferred"):
                PRESENCE["deferred"] = True
                try:
                    room.post("system",
                              f"还有 {len(pending)} 个任务在跑，干完再收工（网页已经关了也不打断）",
                              "event", {"phase": "collab", "state": "ok", "actor": "deepseek"})
                except Exception:  # noqa: BLE001
                    pass
            continue          # 任务没结束，先不收工

        _shutdown(trigger)


def _typing_info() -> dict:
    try:
        data = json.loads((ROOM / "typing.json").read_text(encoding="utf-8"))
        if time.time() - float(data.get("at") or 0) > 180:   # 超过 3 分钟算超时，别一直转
            return {}
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _project_info() -> dict:
    try:
        return json.loads((ROOM / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tasks_summary() -> list[dict]:
    out: list[dict] = []
    if not TASKS.is_dir():
        return out
    for directory in TASKS.iterdir():
        card = directory / "task.json"
        if not card.is_file():
            continue
        try:
            task = json.loads(card.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": task.get("id", directory.name),
            "title": task.get("title", ""),
            "status": task.get("status", "planned"),
            "rounds": len(task.get("rounds") or []),
            "at": task.get("updated_at", task.get("created_at", "")),
        })
    out.sort(key=lambda t: (t["at"], t["id"]), reverse=True)
    return out


def _tail(path: Path, lines: int = 22) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:]).strip()


def _task_detail(tid: str) -> dict | None:
    directory = TASKS / tid
    card = directory / "task.json"
    if not card.is_file():
        return None
    try:
        task = json.loads(card.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    def newest(pattern: str) -> Path | None:
        """按修改时间取最新、且非空的文件（按名字排序会被 01 与 01-0 坑到）。"""
        items = [p for p in directory.glob(pattern) if p.is_file() and p.stat().st_size > 0]
        return max(items, key=lambda p: p.stat().st_mtime) if items else None

    verifies = [newest("verify-*.txt")]
    reviews = [newest("review-*.last.md")]
    detail = {
        "id": task.get("id", tid),
        "title": task.get("title", ""),
        "goal": task.get("goal", ""),
        "status": task.get("status", "planned"),
        "workspace": task.get("workspace", ""),
        "acceptance": task.get("acceptance") or [],
        "rounds": task.get("rounds") or [],
        "reviews": task.get("reviews") or [],
        "verify_tail": _tail(verifies[0]) if verifies[0] else "",
        "review_text": _tail(reviews[0], 26) if reviews[0] else "",
        "diff_stat": "",
    }
    workspace = Path(task.get("workspace") or ROOT)
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat", "HEAD"], cwd=str(workspace),
            capture_output=True, encoding="utf-8", errors="replace", timeout=4, creationflags=quiet_flags())
        stat = (proc.stdout or "").strip()
        if not stat:
            proc = subprocess.run(
                ["git", "status", "--short"], cwd=str(workspace),
                capture_output=True, encoding="utf-8", errors="replace", timeout=4, creationflags=quiet_flags())
            stat = (proc.stdout or "").strip()
        detail["diff_stat"] = stat[-1200:] or "工作区干净（没有未提交改动）"
    except (OSError, subprocess.SubprocessError):
        pass
    return detail


def _agents_state(tasks: list[dict]) -> dict:
    """从消息流里最近的事件推断各成员状态。"""
    state = {"deepseek": "idle", "codex": "idle", "reviewer": "idle", "dispatch": "idle",
             "verify": "idle", "review": "idle"}
    started: dict[str, float] = {}
    events = [m for m in room.read_all() if m.get("kind") == "event"][-40:]
    for msg in events:
        meta = msg.get("meta") or {}
        phase = meta.get("phase")
        label = {"dispatch": "dispatch", "verify": "verify", "review": "review"}.get(phase)
        if not label:
            continue
        if meta.get("state") == "run":
            started[label] = 1
        elif meta.get("state") in ("ok", "bad"):
            started.pop(label, None)
    for label, flag in (("dispatch", "codex"), ("verify", "deepseek"), ("review", "reviewer")):
        if started.get(label):
            state[flag] = "working"
            state[label] = "working"
    return state


class Handler(BaseHTTPRequestHandler):
    server_version = "RoomWorkbench/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            page = (PAGE
                    .replace("__AGENTS__", json.dumps(AGENTS, ensure_ascii=False))
                    .replace("__STATUS_LABEL__", json.dumps(STATUS_LABEL, ensure_ascii=False)))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            PRESENCE["last_seen"] = time.time()
            PRESENCE["ever"] = True
            PRESENCE["bye_at"] = 0.0        # 刷新页面会重新发请求，撤销关闭标记
            PRESENCE["deferred"] = False
            qs = parse_qs(parsed.query)
            after_id = int((qs.get("after") or ["0"])[0] or 0)
            tid = (qs.get("task") or [""])[0]
            all_msgs = room.read_all()
            tasks = _tasks_summary()
            payload = {
                "messages": [m for m in all_msgs if m["id"] > after_id],
                "total_messages": len(all_msgs),
                "tasks": tasks,
                "task": _task_detail(tid) if tid else None,
                "agents": _agents_state(tasks),
                "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "root": str(ROOT),
                "project": _project_info(),
                "typing": _typing_info(),
            }
            self._json(payload)
            return
        if parsed.path == "/api/alive":
            PRESENCE["last_seen"] = time.time()
            PRESENCE["ever"] = True
            PRESENCE["bye_at"] = 0.0
            self._json({"ok": True})
            return
        if parsed.path == "/api/messages":
            qs = parse_qs(parsed.query)
            after_id = int((qs.get("after") or ["0"])[0] or 0)
            self._json(room.after(after_id))
            return
        if parsed.path == "/api/tasks":
            self._json(_tasks_summary())
            return
        if parsed.path == "/api/task":
            qs = parse_qs(parsed.query)
            tid = (qs.get("id") or [""])[0]
            detail = _task_detail(tid)
            self._json(detail if detail else {"error": "not found"}, 200 if detail else 404)
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/bye":
            PRESENCE["bye_at"] = time.time()
            self._json({"ok": True, "bye_at": PRESENCE["bye_at"]})
            return
        if parsed.path != "/api/post":
            self._send(404, b'{"error":"not found"}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}')
            return
        author = payload.get("as") or "user"
        text = (payload.get("text") or "").strip()
        if not text:
            self._send(400, b'{"error":"empty text"}')
            return
        msg = room.post(author, text, payload.get("kind") or "msg", payload.get("meta"))
        try:
            (ROOM / "nudge").write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        self._json(msg)


def main() -> int:
    ap = argparse.ArgumentParser(description="多 Agent 协作台")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-autostop", action="store_true",
                    help="关网页也不收工（常驻后台用）")
    ap.add_argument("--idle-seconds", type=float, default=90.0,
                    help="多久没收到页面心跳就收工（默认 90 秒）")
    ap.add_argument("--grace-seconds", type=float, default=5.0,
                    help="收到关闭通知后等待几秒，留时间给刷新页面")
    args = ap.parse_args()

    autostop = not args.no_autostop and os.environ.get("AGENT_TEAM_AUTOSTOP", "1") != "0"
    _write_session_state("running")
    if autostop:
        threading.Thread(target=_guardian, args=(args.idle_seconds, args.grace_seconds),
                         daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"协作台: http://{args.host}:{args.port}/", flush=True)
    print("关掉网页就自动收工" if autostop else "自动收工已关闭：关网页后仍在后台跑", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())