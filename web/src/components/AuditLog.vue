<script setup>
// The audit trail for compliance review: filter by person, kind of event, dates and text; newest first;
// export exactly what is filtered as CSV. Read-only - the trail itself is append-only in the database.
import { reactive, ref, computed, watch, onMounted } from 'vue'

const props = defineProps({
  staff: { type: Array, default: () => [] },
  groups: { type: Array, default: () => [] },
})

const f = reactive({ actor: '', group: '', from: '', to: '', q: '' })
const rows = ref([])
const next = ref(null)
const loading = ref(false)
const error = ref('')
const status = ref('')
const expanded = ref(new Set())
let timer = null
let ctrl = null

const query = computed(() => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(f)) if (v) p.set(k, v)
  return p
})
const csvHref = computed(() => `/audit.csv${query.value.toString() ? '?' + query.value : ''}`)
const filtered = computed(() => Object.values(f).some(Boolean))

async function load(more = false) {
  if (ctrl) ctrl.abort()
  ctrl = new AbortController()
  const p = new URLSearchParams(query.value)
  if (more && next.value) p.set('before', next.value)
  loading.value = true
  error.value = ''
  try {
    const res = await fetch(`/api/audit?${p}`, { signal: ctrl.signal, headers: { Accept: 'application/json' },
                                                 credentials: 'same-origin' })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.error || `The log could not be loaded (${res.status})`)
    rows.value = more ? rows.value.concat(data.rows) : data.rows
    next.value = data.next
    if (!more) expanded.value = new Set()
    status.value = `${rows.value.length} entr${rows.value.length === 1 ? 'y' : 'ies'} shown` +
                   (data.next ? ', older entries available' : '') + '.'
  } catch (ex) {
    if (ex.name !== 'AbortError') { error.value = ex.message; status.value = ex.message }
  } finally {
    loading.value = false
  }
}

watch(() => [f.actor, f.group, f.from, f.to], () => load())
watch(() => f.q, () => { clearTimeout(timer); timer = setTimeout(() => load(), 250) })
onMounted(() => load())

function clear() { Object.assign(f, { actor: '', group: '', from: '', to: '', q: '' }) }
function toggle(seq) {
  const s = new Set(expanded.value)
  s.has(seq) ? s.delete(seq) : s.add(seq)
  expanded.value = s
}
const when = (at) => at.replace('T', ' ')
</script>

<template>
  <form class="rx-audit__filters" role="search" aria-label="Filter the audit log" @submit.prevent="load()">
    <div><label for="af-actor">Person</label>
      <select id="af-actor" v-model="f.actor">
        <option value="">Everyone</option>
        <option v-for="s in staff" :key="s" :value="s">{{ s }}</option>
      </select></div>
    <div><label for="af-group">Kind of event</label>
      <select id="af-group" v-model="f.group">
        <option value="">All events</option>
        <option v-for="g in groups" :key="g.value" :value="g.value">{{ g.label }}</option>
      </select></div>
    <div><label for="af-from">From</label><input id="af-from" v-model="f.from" type="date"></div>
    <div><label for="af-to">To</label><input id="af-to" v-model="f.to" type="date"></div>
    <div class="rx-audit__q"><label for="af-q">Search reference or details</label>
      <input id="af-q" v-model="f.q" type="text" placeholder="e.g. H-EXAMPLE-001, refused, REGN-EXAMPLE"></div>
  </form>
  <p class="rx-audit__bar">
    <button v-if="filtered" type="button" class="secondary" @click="clear">Clear filters</button>
    <a class="rx-audit__csv" :href="csvHref" download>Download these entries as CSV</a>
    <span role="status" aria-live="polite" class="muted">{{ loading ? 'Loading…' : status }}</span>
  </p>
  <p v-if="error" class="warn" role="alert">{{ error }}</p>

  <div class="rx-audit__wrap">
    <table class="rx-audit__table" :aria-busy="loading ? 'true' : 'false'">
      <caption class="rx-sr">Audit log, newest first</caption>
      <thead><tr><th scope="col">When</th><th scope="col">Who</th><th scope="col">Event</th><th scope="col">What happened</th>
        <th scope="col"><span class="rx-sr">Details</span></th></tr></thead>
      <tbody>
        <template v-for="row in rows" :key="row.seq">
          <tr :class="{ 'is-override': row.override }">
            <td class="rx-nowrap">{{ when(row.at) }}</td>
            <th scope="row">{{ row.actor }}</th>
            <td>{{ row.label }}<span v-if="row.override" class="pill rx-audit__ov">Override</span></td>
            <td>{{ row.summary }}<span v-if="row.ref" class="rx-audit__ref"> · {{ row.ref }}</span></td>
            <td><button v-if="row.detail" type="button" class="secondary rx-audit__more"
                        :aria-expanded="expanded.has(row.seq) ? 'true' : 'false'" :aria-controls="`ad-${row.seq}`"
                        @click="toggle(row.seq)">{{ expanded.has(row.seq) ? 'Hide' : 'Details' }}
                        <span class="rx-sr">for entry {{ row.seq }}</span></button></td>
          </tr>
          <tr v-if="expanded.has(row.seq)" :id="`ad-${row.seq}`" class="rx-audit__detail">
            <td colspan="5"><pre>{{ JSON.stringify(row.detail, null, 2) }}</pre>
              <span class="muted">Entry #{{ row.seq }}</span></td>
          </tr>
        </template>
        <tr v-if="!rows.length && !loading"><td colspan="5" class="muted">No entries match these filters.</td></tr>
      </tbody>
    </table>
  </div>
  <p v-if="next"><button type="button" class="secondary" :disabled="loading" @click="load(true)">Load older entries</button></p>
</template>

<style>
.rx-audit__filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 0 12px }
.rx-audit__q { grid-column: 1 / -1 }
.rx-audit__bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin: 12px 0 }
.rx-audit__wrap { overflow-x: auto }
.rx-audit__table th[scope="row"] { font-weight: 600 }
.rx-audit__table tr.is-override > * { background: var(--amber-bg) }
.rx-audit__ov { margin-left: 8px; color: var(--amber); border-color: var(--amber) }
.rx-audit__ref { color: var(--muted); word-break: break-all }
.rx-audit__more { padding: 4px 10px }
.rx-audit__detail pre { margin: 0 0 4px; white-space: pre-wrap; word-break: break-word; font-size: .9em }
.rx-nowrap { white-space: nowrap }
</style>
