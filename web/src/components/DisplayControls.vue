<script setup>
// Header controls on every page: text size, contrast, and the keyboard shortcut list.
import { computed, ref, watch, nextTick } from 'vue'
import { prefs, TEXT_SIZES, osWantsMoreContrast } from '../prefs.js'
import { helpOpen, announcement, modLabel } from '../shortcuts.js'

const dialog = ref(null)
const size = computed(() => TEXT_SIZES[prefs.text])
const highContrast = computed(() => prefs.contrast === 'hc' || (prefs.contrast === 'auto' && osWantsMoreContrast()))

function bigger() { if (prefs.text < TEXT_SIZES.length - 1) prefs.text++ }
function smaller() { if (prefs.text > 0) prefs.text-- }
function toggleContrast() { prefs.contrast = highContrast.value ? 'std' : 'hc' }

watch(helpOpen, async (open) => {
  await nextTick()
  if (open && dialog.value && !dialog.value.open) dialog.value.showModal()
  if (!open && dialog.value && dialog.value.open) dialog.value.close()
})

const keys = [
  [`${modLabel} + U`, 'Choose a prescription photo (on a finished case: go to the next customer)'],
  [`${modLabel} + Enter`, 'Check the photo / get the decision - the same as pressing the main button'],
  ['↓ ↑ then Enter', 'Pick a suggestion in a lookup field; Esc closes the list'],
  ['?', 'Show this list'],
]
</script>

<template>
  <div class="rx-display" role="group" aria-label="Display and keyboard">
    <span class="rx-display__size" aria-live="polite">Text {{ size }}%</span>
    <button type="button" class="secondary rx-icon" :disabled="prefs.text === 0" @click="smaller"
            aria-label="Smaller text">A−</button>
    <button type="button" class="secondary rx-icon" :disabled="prefs.text === TEXT_SIZES.length - 1" @click="bigger"
            aria-label="Larger text">A+</button>
    <button type="button" class="secondary" :aria-pressed="highContrast ? 'true' : 'false'" @click="toggleContrast">
      High contrast
    </button>
    <button type="button" class="secondary" aria-keyshortcuts="Shift+?" @click="helpOpen = true">Shortcuts</button>
  </div>

  <Teleport to="body">
    <dialog ref="dialog" class="rx-dialog" aria-labelledby="rx-keys-title" @close="helpOpen = false">
      <h2 id="rx-keys-title">Keyboard shortcuts</h2>
      <table>
        <caption class="rx-sr">Shortcut keys and what they do</caption>
        <thead><tr><th scope="col">Keys</th><th scope="col">Does</th></tr></thead>
        <tbody>
          <tr v-for="[k, what] in keys" :key="k"><td><kbd>{{ k }}</kbd></td><td>{{ what }}</td></tr>
        </tbody>
      </table>
      <p class="muted">Recording a hold outcome (confirmed, refused, override) never has a shortcut: choose it on purpose.</p>
      <form method="dialog"><button type="submit" autofocus>Close</button></form>
    </dialog>
    <p class="rx-sr" role="status" aria-live="polite">{{ announcement }}</p>
  </Teleport>
</template>

<style>
.rx-display { display: flex; gap: 6px; align-items: center; flex-wrap: wrap }
.rx-display button { padding: 6px 10px }
.rx-display__size { color: var(--muted); font-size: .9rem; min-width: 5.5em; text-align: right }
.rx-icon { min-width: 2.75rem; font-weight: 800 }
.rx-display button[aria-pressed="true"] { background: var(--accent); color: var(--card) }
.rx-display button:disabled { opacity: 1; color: var(--muted); border-color: var(--line); cursor: default }
.rx-dialog { max-width: 560px; width: calc(100% - 32px); border: 2px solid var(--ink); border-radius: 12px;
  background: var(--card); color: var(--ink); padding: 20px }
.rx-dialog::backdrop { background: rgb(0 0 0 / .6) }
kbd { font: .9em ui-monospace, Consolas, monospace; padding: 1px 6px; border: 1px solid var(--ink);
  border-bottom-width: 2px; border-radius: 4px; background: var(--bg); color: var(--ink); white-space: nowrap }
.rx-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap }
</style>
