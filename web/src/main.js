// Enhancements on top of the server-rendered counter pages. Every page works without this file; when it
// loads it adds the display controls, the hotkeys, lookup fields and the interactive audit log.
import { createApp } from 'vue'
import DisplayControls from './components/DisplayControls.vue'
import LookupField from './components/LookupField.vue'
import AuditLog from './components/AuditLog.vue'
import { installShortcuts } from './shortcuts.js'
import { installQuickCheck } from './quickcheck.js'

document.documentElement.classList.add('js')
installShortcuts()
installQuickCheck()

const display = document.getElementById('rx-display')
if (display) createApp(DisplayControls).mount(display)

// <div data-lookup="drug" data-multi> <input ...> </div>  ->  the same input, with suggestions
document.querySelectorAll('[data-lookup]').forEach((el) => {
  const inp = el.querySelector('input')
  if (!inp) return
  const label = document.querySelector(`label[for="${inp.id}"]`)
  const attrs = {
    id: inp.id, name: inp.name, value: inp.value, type: inp.type, placeholder: inp.placeholder || undefined,
    required: inp.required, inputmode: inp.getAttribute('inputmode') || undefined,
    describedby: inp.getAttribute('aria-describedby') || undefined,
    label: label ? label.textContent.trim() : inp.name,
  }
  createApp(LookupField, { kind: el.dataset.lookup, multi: 'multi' in el.dataset, attrs }).mount(el)
})

const audit = document.getElementById('rx-audit')
if (audit) {
  createApp(AuditLog, { staff: JSON.parse(audit.dataset.staff || '[]'),
                        groups: JSON.parse(audit.dataset.groups || '[]') }).mount(audit)
}
