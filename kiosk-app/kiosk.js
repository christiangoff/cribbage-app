'use strict';

// ── Server URL — injected via ?server=http://... by the Python launcher ────
const SERVER = new URLSearchParams(window.location.search).get('server') || '';

// ── State ──────────────────────────────────────────────────────────────────
const S = {
  user:       null,   // {id, username, player_id, player_name}
  gameId:     null,
  gs:         null,   // latest game state from /play/<id>/state
  pollTimer:  null,
  selected:   [],     // selected card indices
  prevP1:     0,      // previous p1 score (for back peg)
  prevP2:     0,
};

// ── API ────────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, credentials: 'include', headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(SERVER + path, opts);
    return r.json();
  } catch (e) {
    return { error: 'network_error' };
  }
}
const GET  = p      => api('GET',  p);
const POST = (p, b) => api('POST', p, b);

// ── Boot ───────────────────────────────────────────────────────────────────
async function boot() {
  const me = await GET('/api/me');
  if (me.error) { showLogin(); return; }
  S.user = me;
  showLobby();
}

// ── Helpers ────────────────────────────────────────────────────────────────
function app() { return document.getElementById('app'); }

function stopPoll() {
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
}

function cardHtml(card, idx, clickable, selected, unplayable) {
  const suit = card.slice(-1);
  const rank = card.slice(0, -1);
  const sym  = { H: '♥', D: '♦', C: '♣', S: '♠' }[suit] || suit;
  const cls  = ['H','D'].includes(suit) ? 'card-r' : 'card-b';
  const sel  = selected  ? ' selected'   : '';
  const unp  = unplayable ? ' unplayable' : '';
  const onclick = clickable ? `onclick="cardClick(${idx})"` : '';
  return `<div class="card${sel}${unp}" data-idx="${idx}" ${onclick}><span class="card-inner ${cls}">${rank}${sym}</span></div>`;
}

function rankVal(card) {
  const r = card.slice(0, -1);
  if (['T','J','Q','K'].includes(r)) return 10;
  if (r === 'A') return 1;
  return parseInt(r);
}

function phaseLabel(p) {
  return { lobby:'Waiting', discarding:'Discard', cutting:'Cut', pegging:'Pegging', counting:'Count', complete:'Done' }[p] || p;
}

// ── LOGIN ──────────────────────────────────────────────────────────────────
function showLogin(err) {
  stopPoll();
  S.gameId = null; S.gs = null; S.selected = [];
  app().innerHTML = `
    <div class="view-login">
      <h1>🃏 Family Cribbage</h1>
      <p class="subtitle">Sign in to play</p>
      <div class="login-box">
        <input id="u" type="text" placeholder="Username" autocomplete="off" autocapitalize="off" autocorrect="off">
        <input id="p" type="password" placeholder="Password">
        ${err ? `<div class="error-msg">${err}</div>` : ''}
        <button class="btn-primary" id="btn-login">Log In</button>
      </div>
    </div>`;
  const doLogin = async () => {
    const u = document.getElementById('u').value.trim();
    const p = document.getElementById('p').value;
    if (!u || !p) return;
    const d = await POST('/api/login', { username: u, password: p });
    if (d.ok) { S.user = d; showLobby(); }
    else showLogin(d.error || 'Login failed');
  };
  document.getElementById('btn-login').onclick = doLogin;
  document.getElementById('p').onkeydown = e => { if (e.key === 'Enter') doLogin(); };
  document.getElementById('u').focus();
}

// ── LOBBY ──────────────────────────────────────────────────────────────────
async function showLobby() {
  stopPoll();
  S.gameId = null; S.gs = null; S.selected = [];

  const lobby = await GET('/api/lobby');

  const noPlayer = !S.user.player_id;

  const myGamesHtml = (lobby.my_games || []).map(g => `
    <div class="game-row">
      <div class="game-row-info">
        <span class="opp">${g.waiting ? 'Waiting for opponent…' : (g.opponent ? `vs ${g.opponent}` : 'Game')}</span>
        <span class="phase">${phaseLabel(g.phase)}</span>
      </div>
      <button class="btn-ghost" onclick="enterGame(${g.id})">Resume</button>
    </div>`).join('');

  const openHtml = (lobby.open_games || []).map(g => `
    <div class="game-row">
      <div class="game-row-info"><span class="opp">${g.creator} is waiting</span></div>
      <button class="btn-primary" onclick="joinGame(${g.id})">Join</button>
    </div>`).join('');

  app().innerHTML = `
    <div class="view-lobby">
      <div class="lobby-bar">
        <div>
          <span class="user-name">${S.user.username}</span>
          ${S.user.player_name
            ? `<span class="player-tag">▶ ${S.user.player_name}</span>`
            : `<span class="no-player">Not linked to a player — ask admin</span>`}
        </div>
        <button class="btn-ghost sm" onclick="doLogout()">Log Out</button>
      </div>
      <div class="lobby-body">
        ${noPlayer ? '' : `
          <div class="lobby-section">
            <button class="btn-primary xl" onclick="newGame()">+ New Game</button>
          </div>`}
        ${myGamesHtml ? `<div class="lobby-section"><h3>Your Games</h3>${myGamesHtml}</div>` : ''}
        ${openHtml    ? `<div class="lobby-section"><h3>Join a Game</h3>${openHtml}</div>`    : ''}
        ${!myGamesHtml && !openHtml && !noPlayer
          ? `<div class="lobby-empty">No active games. Start one!</div>` : ''}
      </div>
    </div>`;

  // Refresh lobby every 5s while waiting
  S.pollTimer = setInterval(async () => { if (!S.gameId) await refreshLobby(); }, 5000);
}

async function refreshLobby() {
  const lobby = await GET('/api/lobby');

  // Quietly update the game lists without full re-render
  const myGamesHtml = (lobby.my_games || []).map(g => `
    <div class="game-row">
      <div class="game-row-info">
        <span class="opp">${g.waiting ? 'Waiting for opponent…' : (g.opponent ? `vs ${g.opponent}` : 'Game')}</span>
        <span class="phase">${phaseLabel(g.phase)}</span>
      </div>
      <button class="btn-ghost" onclick="enterGame(${g.id})">Resume</button>
    </div>`).join('');

  const openHtml = (lobby.open_games || []).map(g => `
    <div class="game-row">
      <div class="game-row-info"><span class="opp">${g.creator} is waiting</span></div>
      <button class="btn-primary" onclick="joinGame(${g.id})">Join</button>
    </div>`).join('');

  // If someone joined our waiting game, auto-enter it
  for (const g of (lobby.my_games || [])) {
    if (g.phase === 'discarding' && !g.waiting) {
      // New game just got a second player — jump in
      enterGame(g.id);
      return;
    }
  }
}

async function doLogout() {
  await POST('/api/logout');
  S.user = null;
  showLogin();
}

async function newGame() {
  const d = await POST('/api/games/new');
  if (d.ok) enterGame(d.game_id);
}

async function joinGame(id) {
  const d = await POST(`/api/games/${id}/join`);
  if (d.ok) enterGame(d.game_id);
  else alert(d.error || 'Could not join');
}

function enterGame(id) {
  S.gameId = id;
  S.selected = [];
  S.prevP1 = 0; S.prevP2 = 0;
  stopPoll();
  pollGame();
  S.pollTimer = setInterval(pollGame, 1500);
}

// ── GAME ───────────────────────────────────────────────────────────────────
async function pollGame() {
  const d = await GET(`/play/${S.gameId}/state`);
  if (!d.phase) return;

  // Track back-peg positions
  if (S.gs) {
    if (d.player1_score !== S.gs.player1_score) S.prevP1 = S.gs.player1_score;
    if (d.player2_score !== S.gs.player2_score) S.prevP2 = S.gs.player2_score;
  }
  S.gs = d;
  renderGame(d);
}

function renderGame(gs) {
  const r = gs.my_role; // 'p1' | 'p2' | null
  const myScore  = r === 'p1' ? gs.player1_score : gs.player2_score;
  const oppScore = r === 'p1' ? gs.player2_score : gs.player1_score;
  const myName   = r === 'p1' ? gs.player1_name  : gs.player2_name;
  const oppName  = r === 'p1' ? gs.player2_name  : gs.player1_name;

  app().innerHTML = `
    <div class="view-game">
      <div class="game-header">
        <div class="score-panel mine">
          <span class="pname">${myName || '—'} (You)</span>
          <span class="pscore">${myScore}</span>
        </div>
        <div class="game-center">
          <div class="phase-tag">${phaseLabel(gs.phase)}</div>
          ${gs.dealer_name ? `<div class="dealer-tag">Dealer: ${gs.dealer_name}</div>` : ''}
        </div>
        <div class="score-panel theirs">
          <span class="pname">${oppName || 'Waiting…'}</span>
          <span class="pscore">${oppScore}</span>
        </div>
        <button class="btn-link" onclick="leaveGame()">✕ Leave</button>
      </div>

      <canvas id="board-canvas" width="800" height="160"></canvas>

      <div class="info-bar">
        ${gs.starter ? `<span class="starter">Cut: ${inlineCard(gs.starter)}</span>` : '<span></span>'}
        <span class="events">${(gs.events || []).slice(-2).join(' · ')}</span>
      </div>

      <div class="play-area" id="play-area"></div>
    </div>`;

  drawBoard(
    document.getElementById('board-canvas'),
    { p1: { front: gs.player1_score, back: S.prevP1 },
      p2: { front: gs.player2_score, back: S.prevP2 } },
    { p1: gs.player1_name, p2: gs.player2_name },
    r
  );

  renderPlayArea(gs, r);
}

function renderPlayArea(gs, r) {
  const el = document.getElementById('play-area');
  if (!el) return;

  if (gs.phase === 'lobby') {
    el.innerHTML = `<div class="wait-block"><div class="spinner"></div><p>Waiting for opponent to join…</p></div>`;
    return;
  }
  if (gs.phase === 'complete') {
    const myId = r === 'p1' ? gs.player1_id : gs.player2_id;
    const won  = gs.winner_id === myId;
    el.innerHTML = `
      <div class="gameover">
        <h2>${won ? '🎉 You Win!' : '😔 You Lose'}</h2>
        <p class="final-scores">${gs.player1_name} ${gs.player1_score} – ${gs.player2_score} ${gs.player2_name}</p>
        <button class="btn-primary lg" onclick="leaveGame()">Back to Lobby</button>
      </div>`;
    return;
  }
  if (gs.phase === 'discarding') { renderDiscard(el, gs, r); return; }
  if (gs.phase === 'cutting')    { renderCut(el, gs, r);     return; }
  if (gs.phase === 'pegging')    { renderPegging(el, gs, r); return; }
  if (gs.phase === 'counting')   { renderCounting(el, gs, r); return; }
  el.innerHTML = `<p style="color:#8b949e">Phase: ${gs.phase}</p>`;
}

// ── Discard ────────────────────────────────────────────────────────────────
function renderDiscard(el, gs, r) {
  const myDisc  = r === 'p1' ? gs.p1_discarded : gs.p2_discarded;
  const oppDisc = r === 'p1' ? gs.p2_discarded : gs.p1_discarded;

  if (myDisc) {
    el.innerHTML = `<div class="wait-block"><div class="spinner"></div><p>${oppDisc ? 'Both discarded — dealing…' : 'Waiting for opponent to discard…'}</p></div>`;
    return;
  }
  const hand = gs.my_hand || [];
  const cardsHtml = hand.map((c, i) => cardHtml(c, i, true, S.selected.includes(i), false)).join('');
  const ready = S.selected.length === 2;

  el.innerHTML = `
    <p class="prompt">Select 2 cards to discard to ${gs.dealer_name}'s crib</p>
    <div class="hand">${cardsHtml}</div>
    <button class="btn-primary lg ${ready ? '' : 'off'}" id="btn-disc" ${ready ? '' : 'disabled'}>
      Discard (${S.selected.length}/2 selected)
    </button>`;
  document.getElementById('btn-disc').onclick = doDiscard;
}

function cardClick(idx) {
  const gs = S.gs;
  if (!gs) return;
  if (gs.phase === 'discarding') {
    const pos = S.selected.indexOf(idx);
    if (pos === -1) { if (S.selected.length < 2) S.selected.push(idx); }
    else S.selected.splice(pos, 1);
    renderPlayArea(gs, gs.my_role);
  } else if (gs.phase === 'pegging') {
    S.selected = [idx];
    renderPlayArea(gs, gs.my_role);
  }
}

async function doDiscard() {
  if (S.selected.length !== 2) return;
  const d = await POST(`/play/${S.gameId}/discard`, { indices: S.selected });
  S.selected = [];
  if (d.ok) pollGame();
}

// ── Cut ────────────────────────────────────────────────────────────────────
function renderCut(el, gs, r) {
  el.innerHTML = `
    <div class="wait-block">
      <p>Cut the deck to reveal the starter card</p>
      <button class="btn-primary xl" onclick="doCut()">Cut Deck</button>
    </div>`;
}

async function doCut() {
  const d = await POST(`/play/${S.gameId}/cut`);
  if (d.ok) pollGame();
}

// ── Pegging ────────────────────────────────────────────────────────────────
function renderPegging(el, gs, r) {
  const pegging  = gs.pegging || {};
  const count    = pegging.count || 0;
  const pile     = pegging.pile  || [];
  const myId     = r === 'p1' ? gs.player1_id : gs.player2_id;
  const isMyTurn = pegging.turn_player_id === myId;
  const hand     = gs.my_hand || [];

  const pileHtml = pile.slice(-6).map(c => `<span class="pile-card">${inlineCard(c)}</span>`).join('');

  const cardsHtml = hand.map((c, i) => {
    const playable = rankVal(c) + count <= 31;
    return cardHtml(c, i, playable && isMyTurn, S.selected.includes(i), !playable);
  }).join('');

  const turnName = pegging.turn_player_id === gs.player1_id ? gs.player1_name : gs.player2_name;

  el.innerHTML = `
    <div class="pile-row">
      <span class="count-big">${count}</span>
      <span class="pile-cards">${pileHtml || '<span style="color:#555">—</span>'}</span>
    </div>
    <div class="hand">${cardsHtml}</div>
    <div class="peg-actions">
      ${isMyTurn
        ? `<button class="btn-primary lg ${S.selected.length !== 1 ? 'off' : ''}" id="btn-peg" ${S.selected.length !== 1 ? 'disabled' : ''}>Play Card</button>
           <button class="btn-ghost" onclick="doGo()">Say Go</button>`
        : `<div class="wait-block"><p>Waiting for ${turnName}…</p></div>`}
    </div>`;
  if (isMyTurn) document.getElementById('btn-peg').onclick = doPeg;
}

async function doPeg() {
  if (S.selected.length !== 1) return;
  const card = S.gs.my_hand[S.selected[0]];
  S.selected = [];
  const d = await POST(`/play/${S.gameId}/peg`, { card });
  if (d.ok) pollGame();
}

async function doGo() {
  const d = await POST(`/play/${S.gameId}/go`);
  if (d.ok) pollGame();
}

// ── Counting ───────────────────────────────────────────────────────────────
function renderCounting(el, gs, r) {
  const sub = gs.counting_subphase;
  let hand = [], label = '';
  if (sub === 'p1_hand') { hand = gs.counting_p1_hand || []; label = gs.player1_name + "'s hand"; }
  else if (sub === 'p2_hand') { hand = gs.counting_p2_hand || []; label = gs.player2_name + "'s hand"; }
  else if (sub === 'crib')    { hand = gs.counting_crib  || []; label = gs.dealer_name  + "'s crib"; }

  const starterCard = gs.starter ? `<div class="card highlight static"><span class="card-inner">${inlineCard(gs.starter)}</span></div>` : '';
  const cardsHtml   = hand.map(c => cardHtml(c, -1, false, false, false)).join('');

  el.innerHTML = `
    <p class="prompt">Counting: ${label}</p>
    <div class="hand">${cardsHtml}${starterCard}</div>
    <button class="btn-primary lg" onclick="doCount()">Score Hand</button>`;
}

async function doCount() {
  const d = await POST(`/play/${S.gameId}/count`);
  if (d.ok) pollGame();
}

// ── Leave ──────────────────────────────────────────────────────────────────
function leaveGame() {
  stopPoll();
  S.gameId = null; S.gs = null; S.selected = [];
  S.prevP1 = 0; S.prevP2 = 0;
  showLobby();
}

// ── Board drawing ──────────────────────────────────────────────────────────
function drawBoard(canvas, scores, names, myRole) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width;   // 800
  const H = canvas.height;  // 160

  const C = {
    bg:    '#1c1008',
    felt:  '#1a3a0f',
    hole:  '#0a0500',
    p1:    '#e05c3a',   // orange-red  (mine)
    p2:    '#3a8ee0',   // blue        (theirs)
    text:  'rgba(255,255,255,0.45)',
    tick:  'rgba(255,255,255,0.15)',
  };

  // Background
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);

  // Felt
  ctx.fillStyle = C.felt;
  roundRect(ctx, 20, 8, W - 40, H - 16, 10);
  ctx.fill();

  // Layout
  const LEFT  = 55;    // x of hole 1 / hole 121
  const RIGHT = W - 35; // x of hole 60 / hole 61
  const SPAN  = RIGHT - LEFT;
  const STEP  = SPAN / 60;  // pixels per hole
  const HOLE_R = 4.5;
  const PEG_R  = 6;

  // Track y positions  (two going-right, gap, two going-left)
  const Y = { a: 40, b: 62, c: 100, d: 122 };
  // a = myRole p1 going right, b = p2 going right
  // c = p2 going left,         d = p1 going left

  // Swap if I'm p2 so my track is always on top
  const myTrack   = myRole === 'p2' ? { fwd: Y.b, back: Y.c } : { fwd: Y.a, back: Y.d };
  const oppTrack  = myRole === 'p2' ? { fwd: Y.a, back: Y.d } : { fwd: Y.b, back: Y.c };
  const myColor   = C.p1;
  const oppColor  = C.p2;

  // Number labels every 10
  ctx.fillStyle = C.text;
  ctx.font = '9px sans-serif';
  ctx.textAlign = 'center';
  for (let n = 10; n <= 60; n += 10) {
    const x = LEFT + (n - 0.5) * STEP;
    ctx.fillText(n, x, Y.a - 14);
  }
  for (let n = 70; n <= 121; n += 10) {
    const x = RIGHT - (n - 61 + 0.5) * STEP;
    ctx.fillText(n, x, Y.d + 20);
  }

  // Divider line between the two tracks
  ctx.strokeStyle = 'rgba(0,0,0,0.4)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(22, 80); ctx.lineTo(W - 22, 80); ctx.stroke();

  // "START" label
  ctx.fillStyle = C.text;
  ctx.font = '8px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('START', LEFT - 22, 78);

  // Tick marks every 5 holes
  ctx.strokeStyle = C.tick;
  ctx.lineWidth = 0.5;
  for (let i = 5; i <= 60; i += 5) {
    const x = LEFT + (i - 0.5) * STEP + STEP / 2;
    ctx.beginPath(); ctx.moveTo(x, Y.a - 9); ctx.lineTo(x, Y.b + 9); ctx.stroke();
    const xb = RIGHT - (i - 5) * STEP - STEP / 2;
    if (xb > LEFT) { ctx.beginPath(); ctx.moveTo(xb, Y.c - 9); ctx.lineTo(xb, Y.d + 9); ctx.stroke(); }
  }

  // Draw holes
  function hole(x, y) {
    ctx.beginPath(); ctx.arc(x, y, HOLE_R, 0, Math.PI * 2);
    ctx.fillStyle = C.hole; ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.6)'; ctx.lineWidth = 0.8; ctx.stroke();
  }

  // Start holes (before hole 1)
  const startX = LEFT - STEP * 0.8;
  hole(startX, Y.a); hole(startX, Y.b); hole(startX, Y.c); hole(startX, Y.d);

  for (let i = 1; i <= 60; i++) {
    const x = LEFT + (i - 0.5) * STEP;
    hole(x, Y.a); hole(x, Y.b);
  }
  for (let i = 61; i <= 121; i++) {
    const x = RIGHT - (i - 61 + 0.5) * STEP;
    hole(x, Y.c); hole(x, Y.d);
  }

  // Compute peg x/y
  function pegPos(score, track) {
    if (score <= 0) return { x: startX, y: track.fwd };
    if (score <= 60) return { x: LEFT + (score - 0.5) * STEP, y: track.fwd };
    return { x: RIGHT - (score - 61 + 0.5) * STEP, y: track.back };
  }

  function drawPeg(score, track, color, isFront) {
    const { x, y } = pegPos(score, track);
    ctx.beginPath(); ctx.arc(x, y, PEG_R, 0, Math.PI * 2);
    if (isFront) {
      ctx.fillStyle = color; ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
    } else {
      ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.stroke();
    }
  }

  // Back pegs first, then front pegs on top
  const myScores  = myRole === 'p2' ? scores.p2 : scores.p1;
  const oppScores = myRole === 'p2' ? scores.p1 : scores.p2;

  drawPeg(myScores.back,  myTrack,  myColor,  false);
  drawPeg(oppScores.back, oppTrack, oppColor, false);
  drawPeg(myScores.front, myTrack,  myColor,  true);
  drawPeg(oppScores.front,oppTrack, oppColor, true);

  // Player name labels
  ctx.font = 'bold 10px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillStyle = myColor;
  ctx.fillText((names[myRole === 'p2' ? 'p2' : 'p1'] || '').substring(0, 10), 22, myTrack.fwd + 4);
  ctx.fillStyle = oppColor;
  ctx.fillText((names[myRole === 'p2' ? 'p1' : 'p2'] || '').substring(0, 10), 22, oppTrack.fwd + 4);
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function inlineCard(card) {
  if (!card) return '';
  const suit = card.slice(-1);
  const rank = card.slice(0, -1);
  const sym  = { H: '♥', D: '♦', C: '♣', S: '♠' }[suit] || suit;
  const col  = ['H','D'].includes(suit) ? '#c0392b' : '#1a1a1a';
  return `<span style="color:${col};font-weight:bold">${rank}${sym}</span>`;
}

// ── Start ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', boot);
