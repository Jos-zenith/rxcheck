<script setup>
// An input with suggestions from this pharmacy's own data (ARIA 1.2 combobox with a listbox popup).
// It replaces the server-rendered <input> it was mounted on and keeps its id, name, value and validation,
// so the label and the form post are unchanged.
//
// Safety: a suggestion fills only its own field. Choosing a doctor never fills the Regn No (nor the other way
// round) - the prescriber check compares the two as written on the slip. Medicine suggestions are names only:
// the strength is always typed from the slip.
import { ref, computed, onBeforeUnmount } from 'vue'

const props = defineProps({
  kind: { type: String, required: true },          // drug | regn | doctor | patient
  multi: { type: Boolean, default: false },        // comma-separated list: complete the item at the cursor
  attrs: { type: Object, required: true },         // id, name, value, placeholder, required, type, inputmode
})

const value = ref(props.attrs.value || '')
const items = ref([])
const open = ref(false)
const active = ref(-1)
const status = ref('')
const input = ref(null)
const listId = `${props.attrs.id}-suggestions`
const minChars = props.kind === 'regn' ? 1 : 2
let timer = null
let ctrl = null

const activeId = computed(() => (open.value && active.value >= 0 ? `${listId}-${active.value}` : undefined))

function segment() {
  // the item being typed: between the separators around the cursor
  const v = value.value
  if (!props.multi) return { start: 0, end: v.length, text: v.trim() }
  const caret = input.value ? input.value.selectionStart ?? v.length : v.length
  const before = v.slice(0, caret)
  const start = Math.max(before.lastIndexOf(','), before.lastIndexOf(';')) + 1
  const rest = v.slice(caret).search(/[,;]/)
  const end = rest < 0 ? v.length : caret + rest
  return { start, end, text: v.slice(start, end).trim() }
}

async function fetchItems() {
  const q = segment().text
  if (props.multi && /\d/.test(q)) { close(); return }   // past the name, typing the strength: stay out of the way
  if (q.length < minChars) { close(); return }
  if (ctrl) ctrl.abort()
  ctrl = new AbortController()
  try {
    const res = await fetch(`/api/lookup?kind=${encodeURIComponent(props.kind)}&q=${encodeURIComponent(q)}`,
                            { signal: ctrl.signal, headers: { Accept: 'application/json' }, credentials: 'same-origin' })
    if (!res.ok) { close(); return }
    const data = await res.json()
    items.value = data.items || []
    active.value = -1
    open.value = items.value.length > 0
    status.value = items.value.length
      ? `${items.value.length} suggestion${items.value.length === 1 ? '' : 's'}. Use the down arrow to choose.`
      : 'No suggestions.'
  } catch (ex) {
    if (ex.name !== 'AbortError') close()
  }
}

function onInput() {
  clearTimeout(timer)
  timer = setTimeout(fetchItems, 120)
}

function close() {
  open.value = false
  active.value = -1
}

function choose(i) {
  const item = items.value[i]
  if (!item) return
  if (props.multi) {
    const { start, end } = segment()
    const v = value.value
    const lead = start > 0 ? ' ' : ''
    const tail = v.slice(end)
    const insert = `${lead}${item.value} `            // a space, so the strength from the slip goes straight after
    value.value = v.slice(0, start) + insert + tail.replace(/^\s+/, '')
    const caret = start + insert.length
    requestAnimationFrame(() => input.value && input.value.setSelectionRange(caret, caret))
  } else {
    value.value = item.value
  }
  status.value = `${item.label} chosen.`
  close()
}

function onKeydown(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return   // leave Ctrl+Enter etc. to the page shortcuts
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (!open.value) { fetchItems(); return }
    active.value = (active.value + 1) % items.value.length
  } else if (e.key === 'ArrowUp') {
    if (!open.value) return
    e.preventDefault()
    active.value = active.value <= 0 ? items.value.length - 1 : active.value - 1
  } else if (e.key === 'Enter' && open.value && active.value >= 0) {
    e.preventDefault()                               // pick the suggestion, do not send the form
    choose(active.value)
  } else if (e.key === 'Escape' && open.value) {
    e.preventDefault()
    close()
  } else if (e.key === 'Tab') {
    close()
  }
}

onBeforeUnmount(() => { clearTimeout(timer); if (ctrl) ctrl.abort() })
</script>

<template>
  <div class="rx-lookup">
    <input ref="input" v-model="value" :id="attrs.id" :name="attrs.name" :type="attrs.type || 'text'"
           :placeholder="attrs.placeholder" :required="attrs.required" :inputmode="attrs.inputmode"
           role="combobox" autocomplete="off" spellcheck="false" aria-autocomplete="list"
           :aria-expanded="open ? 'true' : 'false'" :aria-controls="listId" :aria-activedescendant="activeId"
           :aria-describedby="attrs.describedby" @input="onInput" @keydown="onKeydown" @blur="close">
    <ul v-show="open" :id="listId" role="listbox" class="rx-lookup__list" :aria-label="`Suggestions for ${attrs.label}`">
      <li v-for="(it, i) in items" :id="`${listId}-${i}`" :key="it.value + i" role="option"
          :aria-selected="i === active ? 'true' : 'false'" :class="{ 'is-active': i === active }"
          @mousedown.prevent @click="choose(i)">
        <span class="rx-lookup__label">{{ it.label }}</span>
        <span v-if="it.tag" class="rx-lookup__tag" :class="`tag-${it.tag}`">{{ it.tagText }}</span>
        <span v-if="it.detail" class="rx-lookup__detail">{{ it.detail }}</span>
      </li>
    </ul>
    <span class="rx-sr" role="status" aria-live="polite">{{ status }}</span>
  </div>
</template>

<style>
.rx-lookup { position: relative }
.rx-lookup__list { position: absolute; z-index: 20; left: 0; right: 0; top: calc(100% + 2px); margin: 0; padding: 4px;
  list-style: none; background: var(--card); color: var(--ink); border: 2px solid var(--ink); border-radius: 8px;
  max-height: min(60vh, 22rem); overflow-y: auto; box-shadow: 0 6px 18px rgb(0 0 0 / .25) }
.rx-lookup__list li { margin: 0; padding: 8px 10px; border-radius: 6px; cursor: pointer; display: flex; flex-wrap: wrap;
  gap: 2px 8px; align-items: baseline; border: 2px solid transparent }
.rx-lookup__list li.is-active, .rx-lookup__list li:hover { border-color: var(--accent); background: var(--bg) }
.rx-lookup__label { font-weight: 700 }
.rx-lookup__detail { flex-basis: 100%; color: var(--muted); font-size: .92em }
.rx-lookup__tag { font-size: .8em; font-weight: 700; padding: 0 8px; border-radius: 99px; border: 1px solid }
.rx-lookup__tag.tag-high { color: var(--red); background: var(--red-bg) }
.rx-lookup__tag.tag-verified { color: var(--green); background: var(--green-bg) }
.rx-lookup__tag.tag-unverified { color: var(--amber); background: var(--amber-bg) }
</style>
