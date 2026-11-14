// Live co-editing for the Skills Content Engine.
// mountDoc: a shared document (like Word or Google Docs in edit mode) with live cursors and presence.
// mountGrid: a shared table (like Google Sheets in edit mode) with live cell edits and presence.
// Both sync through a Yjs room over a WebSocket; the server relays and persists the room.
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import Collaboration from '@tiptap/extension-collaboration'
import CollaborationCursor from '@tiptap/extension-collaboration-cursor'
import Image from '@tiptap/extension-image'
import Placeholder from '@tiptap/extension-placeholder'

const PURPLES = ['#4404BF', '#7D5BD9', '#3E0AB7', '#24085F', '#9B7BEA', '#5B2BD6', '#6A3FD9', '#2F0A86']
export function colorFor(name) {
  let h = 0
  for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return PURPLES[h % PURPLES.length]
}

function wsBase() {
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/collab'
}

function debounce(fn, ms) {
  let t
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms) }
}

function renderPresence(provider, el, selfId) {
  if (!el) return
  const states = [...provider.awareness.getStates().entries()]
  const seen = new Set()
  const people = []
  for (const [id, s] of states) {
    if (!s.user || seen.has(s.user.name)) continue
    seen.add(s.user.name)
    people.push({ ...s.user, self: id === selfId })
  }
  el.innerHTML = ''
  const label = document.createElement('span')
  label.className = 'presence-label'
  label.textContent = people.length > 1 ? `${people.length} people here now` : 'Only you here'
  el.appendChild(label)
  for (const p of people) {
    const b = document.createElement('span')
    b.className = 'avatar'
    b.style.background = p.color
    b.title = `${p.name}${p.role ? ' (' + p.role + ')' : ''}${p.self ? ' (you)' : ''}`
    b.setAttribute('aria-label', b.title)
    b.textContent = (p.name || '?').split(/\s+/).map(x => x[0]).join('').slice(0, 2).toUpperCase()
    el.appendChild(b)
  }
}

function setStatus(el, text) { if (el) el.textContent = text }

// ---------------------------------------------------------------- shared document
export function mountDoc(el, opts) {
  const ydoc = new Y.Doc()
  const provider = new WebsocketProvider(opts.wsUrl || wsBase(), opts.room, ydoc, { connect: true })
  const user = { name: opts.user.name, role: opts.user.role, color: colorFor(opts.user.name) }
  provider.awareness.setLocalStateField('user', user)
  const editor = new Editor({
    element: el,
    editable: opts.editable !== false,
    extensions: [
      StarterKit.configure({ history: false, heading: { levels: [2, 3] } }),
      Image.configure({ inline: false, allowBase64: true }),
      Placeholder.configure({ placeholder: opts.placeholder || 'Start writing. Use Screen heading to start a new screen.' }),
      Collaboration.configure({ document: ydoc }),
      CollaborationCursor.configure({ provider, user }),
    ],
    editorProps: { attributes: { class: 'doc-body', 'aria-label': opts.label || 'Document editor', role: 'textbox', 'aria-multiline': 'true' } },
  })
  const changed = debounce(() => opts.onChange && opts.onChange(editor.getHTML()), opts.saveDelay || 2500)
  let seeded = false, seeding = false
  provider.on('sync', (ok) => {
    if (!ok || seeded) return
    seeded = true
    const frag = ydoc.getXmlFragment('default')
    if (frag.length === 0 && opts.canSeed && opts.seedHtml) { seeding = true; editor.commands.setContent(opts.seedHtml); seeding = false }
    else if (frag.length === 0 && opts.seedHtml) showStatic()
    setStatus(opts.statusEl, 'Connected. Changes save automatically.')
  })
  provider.on('status', (e) => setStatus(opts.statusEl, e.status === 'connected' ? 'Connected. Changes save automatically.' : 'Reconnecting... your edits are kept and will sync.'))
  // autosave only edits made here; remote edits are saved by the person who made them
  // Nobody with edit rights has opened this document yet: show the current content read-only until someone does.
  let staticEl = null
  function showStatic() {
    staticEl = document.createElement('div')
    staticEl.className = 'doc-body'
    staticEl.setAttribute('aria-label', (opts.label || 'Document') + ' (current revision)')
    staticEl.innerHTML = opts.seedHtml
    el.style.display = 'none'
    el.parentNode.insertBefore(staticEl, el)
    const frag = ydoc.getXmlFragment('default')
    const off = () => { if (frag.length > 0 && staticEl) { staticEl.remove(); staticEl = null; el.style.display = ''; frag.unobserveDeep(off) } }
    frag.observeDeep(off)
  }
  editor.on('update', ({ transaction }) => { const y = transaction.getMeta('y-sync$'); if (!seeding && !(y && y.isChangeOrigin)) changed() })
  const pres = () => renderPresence(provider, opts.presenceEl, ydoc.clientID)
  provider.awareness.on('change', pres)
  pres()
  if (opts.toolbarEl) bindToolbar(opts.toolbarEl, editor)
  return { editor, provider, ydoc, html: () => editor.getHTML(), insert: (html) => editor.chain().focus().insertContent(html).run() }
}

function bindToolbar(bar, editor) {
  const act = {
    screen: () => editor.chain().focus().toggleHeading({ level: 2 }).run(),
    sub: () => editor.chain().focus().toggleHeading({ level: 3 }).run(),
    para: () => editor.chain().focus().setParagraph().run(),
    bold: () => editor.chain().focus().toggleBold().run(),
    italic: () => editor.chain().focus().toggleItalic().run(),
    bullets: () => editor.chain().focus().toggleBulletList().run(),
    numbers: () => editor.chain().focus().toggleOrderedList().run(),
    quote: () => editor.chain().focus().toggleBlockquote().run(),
    undo: () => editor.chain().focus().undo().run(),
    redo: () => editor.chain().focus().redo().run(),
    media: () => {
      const alt = window.prompt('Describe the image or diagram for screen-reader users (alt text):', '')
      if (alt === null) return
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200"><rect width="640" height="200" fill="#EFE8FF"/><text x="20" y="105" font-family="Poppins,sans-serif" font-size="18" fill="#3E0AB7">Media: ${alt.replace(/[<&>]/g, '').slice(0, 60)}</text></svg>`
      editor.chain().focus().setImage({ src: 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg))), alt }).run()
    },
  }
  bar.querySelectorAll('[data-cmd]').forEach(b => {
    b.addEventListener('click', (e) => { e.preventDefault(); (act[b.dataset.cmd] || (() => {}))() })
  })
  const refresh = () => {
    bar.querySelectorAll('[data-cmd]').forEach(b => {
      const c = b.dataset.cmd
      const on = (c === 'screen' && editor.isActive('heading', { level: 2 })) || (c === 'sub' && editor.isActive('heading', { level: 3 })) ||
        (c === 'bold' && editor.isActive('bold')) || (c === 'italic' && editor.isActive('italic')) ||
        (c === 'bullets' && editor.isActive('bulletList')) || (c === 'numbers' && editor.isActive('orderedList')) || (c === 'quote' && editor.isActive('blockquote'))
      b.setAttribute('aria-pressed', on ? 'true' : 'false')
    })
  }
  editor.on('selectionUpdate', refresh)
  editor.on('transaction', refresh)
}

// ---------------------------------------------------------------- shared grid
export function mountGrid(el, opts) {
  const ydoc = new Y.Doc()
  const provider = new WebsocketProvider(opts.wsUrl || wsBase(), opts.room, ydoc, { connect: true })
  const user = { name: opts.user.name, role: opts.user.role, color: colorFor(opts.user.name) }
  provider.awareness.setLocalStateField('user', user)
  const rows = ydoc.getArray('rows')
  const cols = opts.columns
  const editable = opts.editable !== false
  const changed = debounce(() => opts.onChange && opts.onChange(getRows()), opts.saveDelay || 2000)
  let seeded = false

  function getRows() {
    return rows.toArray().map(m => { const o = {}; m.forEach((v, k) => { o[k] = v }); return o })
  }
  function newId() { return 'r' + Math.random().toString(36).slice(2, 9) }

  const table = document.createElement('table')
  table.className = 'grid'
  const thead = document.createElement('thead')
  const hr = document.createElement('tr')
  const thn = document.createElement('th'); thn.textContent = '#'; thn.className = 'rownum'; hr.appendChild(thn)
  for (const c of cols) {
    const th = document.createElement('th'); th.textContent = c.label; th.scope = 'col'
    if (c.width) { th.style.minWidth = c.width + 'px'; th.style.width = c.width + 'px' }
    if (c.help) th.title = c.help
    hr.appendChild(th)
  }
  if (editable && opts.allowDelete !== false) { const th = document.createElement('th'); th.innerHTML = '<span class="sr-only">Remove</span>'; hr.appendChild(th) }
  thead.appendChild(hr); table.appendChild(thead)
  const tbody = document.createElement('tbody'); table.appendChild(tbody)
  el.innerHTML = ''
  const wrap = document.createElement('div'); wrap.className = 'grid-wrap'; wrap.appendChild(table); el.appendChild(wrap)
  if (editable && opts.allowAdd !== false) {
    const add = document.createElement('button'); add.type = 'button'; add.className = 'btn sec small'; add.textContent = opts.addLabel || 'Add row'
    add.addEventListener('click', () => {
      const m = new Y.Map(); m.set('_id', newId())
      for (const c of cols) m.set(c.key, c.default !== undefined ? (typeof c.default === 'function' ? c.default(getRows()) : c.default) : '')
      rows.push([m])
    })
    el.appendChild(add)
  }

  function control(c, m, rowIdx) {
    const v = m.get(c.key) ?? ''
    let inp
    const ro = !editable || c.readonly
    if (c.type === 'select') {
      inp = document.createElement('select')
      for (const o of (typeof c.options === 'function' ? c.options(getRows()) : c.options)) {
        const op = document.createElement('option'); op.value = o; op.textContent = o; if (String(v) === String(o)) op.selected = true; inp.appendChild(op)
      }
      if (![...inp.options].some(o => o.selected)) { const op = document.createElement('option'); op.value = v; op.textContent = v || '-'; op.selected = true; inp.prepend(op) }
    } else if (c.type === 'textarea') {
      inp = document.createElement('textarea'); inp.rows = 2; inp.value = v
    } else {
      inp = document.createElement('input'); inp.type = c.type === 'number' ? 'number' : 'text'; inp.value = v
      if (c.type === 'number') { inp.step = c.step || 'any' }
    }
    inp.disabled = ro
    inp.dataset.key = c.key
    inp.setAttribute('aria-label', `${c.label}, row ${rowIdx + 1}`)
    const commit = () => {
      let val = inp.value
      if (c.type === 'number') val = val === '' ? '' : Number(val)
      if (m.get(c.key) !== val) ydoc.transact(() => m.set(c.key, val), 'local')
    }
    inp.addEventListener(c.type === 'select' ? 'change' : 'input', commit)
    inp.addEventListener('focus', () => provider.awareness.setLocalStateField('cell', m.get('_id') + ':' + c.key))
    inp.addEventListener('blur', () => provider.awareness.setLocalStateField('cell', null))
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && c.type !== 'textarea' && !e.shiftKey) {
        e.preventDefault()
        const next = tbody.querySelector(`tr[data-idx="${rowIdx + 1}"] [data-key="${c.key}"]`)
        if (next) next.focus()
      }
    })
    return inp
  }

  // Until someone with edit rights opens the room, show the current rows read-only.
  let fallback = null
  function plainMap(r) { const m = new Map(Object.entries(r)); return { get: k => m.get(k), set: () => {}, has: k => m.has(k), forEach: f => m.forEach(f) } }
  function render() {
    const act = document.activeElement
    const focusKey = act && act.dataset && act.dataset.key ? act.closest('tr')?.dataset.id + ':' + act.dataset.key : null
    const selStart = act && 'selectionStart' in act ? act.selectionStart : null
    tbody.innerHTML = ''
    const list = rows.length ? rows.toArray() : (fallback || [])
    list.forEach((m, i) => {
      if (!m.get('_id') && rows.length) m.set('_id', newId())
      const tr = document.createElement('tr'); tr.dataset.id = m.get('_id'); tr.dataset.idx = i
      const tdn = document.createElement('td'); tdn.className = 'rownum'; tdn.textContent = i + 1; tr.appendChild(tdn)
      for (const c of cols) {
        const td = document.createElement('td'); td.dataset.cell = m.get('_id') + ':' + c.key
        td.appendChild(control(c, m, i)); tr.appendChild(td)
      }
      if (editable && opts.allowDelete !== false) {
        const td = document.createElement('td'); const b = document.createElement('button'); b.type = 'button'; b.className = 'icon-btn'
        b.setAttribute('aria-label', `Remove row ${i + 1}`); b.textContent = '×'
        b.addEventListener('click', () => { const idx = rows.toArray().indexOf(m); if (idx >= 0 && window.confirm('Remove this row?')) rows.delete(idx, 1) })
        td.appendChild(b); tr.appendChild(td)
      }
      tbody.appendChild(tr)
    })
    if (focusKey) {
      const [rid, key] = focusKey.split(':')
      const t = tbody.querySelector(`tr[data-id="${rid}"] [data-key="${key}"]`)
      if (t) { t.focus(); if (selStart !== null && 'setSelectionRange' in t) try { t.setSelectionRange(selStart, selStart) } catch (e) {} }
    }
    paintPresence()
  }

  function paintPresence() {
    tbody.querySelectorAll('td[data-cell]').forEach(td => { td.style.boxShadow = ''; td.removeAttribute('data-editor') })
    for (const [id, s] of provider.awareness.getStates().entries()) {
      if (id === ydoc.clientID || !s.cell || !s.user) continue
      const td = tbody.querySelector(`td[data-cell="${s.cell}"]`)
      if (td) { td.style.boxShadow = `inset 0 0 0 2px ${s.user.color}`; td.setAttribute('data-editor', s.user.name) }
    }
    renderPresence(provider, opts.presenceEl, ydoc.clientID)
  }

  rows.observeDeep((events, tr) => {
    const structural = events.some(e => e.target === rows)
    if (structural || tr.origin !== 'local') render()
    if (tr.local && tr.origin !== 'server' && tr.origin !== 'seed') changed()
  })
  provider.awareness.on('change', paintPresence)
  provider.on('sync', (ok) => {
    if (!ok || seeded) return
    seeded = true
    if (rows.length === 0 && opts.canSeed && opts.seedRows && opts.seedRows.length) {
      ydoc.transact(() => {
        for (const r of opts.seedRows) {
          const m = new Y.Map(); m.set('_id', r._id || newId())
          for (const c of cols) m.set(c.key, r[c.key] ?? '')
          for (const k of Object.keys(r)) if (!m.has(k)) m.set(k, r[k])
          rows.push([m])
        }
      }, 'seed')
    }
    if (rows.length === 0 && opts.seedRows && opts.seedRows.length && !opts.canSeed) fallback = opts.seedRows.map(plainMap)
    render()
    setStatus(opts.statusEl, 'Connected. Changes save automatically.')
  })
  provider.on('status', (e) => setStatus(opts.statusEl, e.status === 'connected' ? 'Connected. Changes save automatically.' : 'Reconnecting... your edits are kept and will sync.'))
  render()
  // values the server computes (Lightcast IDs, priority, generated IDs) are written back into the shared rows
  function patch(list) {
    if (!list || !list.length) return
    ydoc.transact(() => {
      const byId = new Map(rows.toArray().map(m => [m.get('_id'), m]))
      for (const p of list) {
        const m = byId.get(p._id); if (!m) continue
        for (const [k, v] of Object.entries(p)) if (k !== '_id' && m.get(k) !== v) m.set(k, v)
      }
    }, 'server')
  }
  return { rows: getRows, provider, ydoc, patch, save: () => opts.onChange && opts.onChange(getRows()) }
}

window.SCE = { mountDoc, mountGrid, colorFor }
