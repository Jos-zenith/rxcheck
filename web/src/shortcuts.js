// Counter hotkeys. The pages mark what the keys act on, so the safety rules stay in the server's HTML:
//   input[data-shortcut="upload"]  Ctrl/Cmd+U focuses it and opens the file picker
//   form[data-primary]             Ctrl/Cmd+Enter submits it - with the browser's own validation, so a
//                                  required field or an unticked "confirm this entry" box still stops it
//   a[data-shortcut="next"]        Ctrl/Cmd+U on a finished case goes to the next customer
// Hold outcomes (confirmed / refused / override) are deliberately never a hotkey.
import { ref } from 'vue'

export const helpOpen = ref(false)
export const announcement = ref('')
export const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
export const modLabel = isMac ? '⌘' : 'Ctrl'

function say(text) {
  announcement.value = ''
  requestAnimationFrame(() => { announcement.value = text })
}

function editable(el) {
  return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))
}

function visible(el) {
  return el && !el.disabled && el.getClientRects().length > 0
}

function focusUpload({ openPicker }) {
  const input = [...document.querySelectorAll('input[data-shortcut="upload"]')].find(visible)
  if (input) {
    input.scrollIntoView({ block: 'center' })
    input.focus()
    if (openPicker) {
      try { input.showPicker ? input.showPicker() : input.click() } catch { input.click() }
    }
    return true
  }
  const next = document.querySelector('a[data-shortcut="next"]')
  if (next) {
    window.location.assign('/#check')
    return true
  }
  say('No photo upload on this page. Finish this entry first, or choose Counter in the menu.')
  return false
}

function submitPrimary() {
  const active = document.activeElement
  const forms = [...document.querySelectorAll('form[data-primary]')].filter(visible)
  const form = (active && active.closest && active.closest('form[data-primary]')) || (forms.length === 1 ? forms[0] : null)
  if (!form) {
    say(forms.length ? 'Move to the form you want to send, then press the shortcut again.'
                     : 'Nothing to submit on this page with the keyboard shortcut.')
    return
  }
  form.requestSubmit()   // runs validation and the submit listeners, exactly like clicking the button
}

export function installShortcuts() {
  window.addEventListener('keydown', (e) => {
    const mod = (isMac ? e.metaKey : e.ctrlKey) && !e.altKey
    if (mod && !e.shiftKey && e.key.toLowerCase() === 'u') {
      e.preventDefault()   // otherwise: the browser's "view source"
      focusUpload({ openPicker: true })
    } else if (mod && e.key === 'Enter') {
      e.preventDefault()
      submitPrimary()
    } else if (e.key === '?' && !mod && !editable(e.target)) {
      e.preventDefault()
      helpOpen.value = true
    }
  })

  // a busy counter double-presses: one check per form, until the page changes
  document.addEventListener('submit', (e) => {
    const form = e.target
    if (form.dataset.sending) { e.preventDefault(); return }
    form.dataset.sending = '1'
    form.setAttribute('aria-busy', 'true')
    form.querySelectorAll('button[type="submit"]').forEach((b) => b.classList.add('is-sending'))
  })
  window.addEventListener('pageshow', () => {   // back button restores the page from cache: allow again
    document.querySelectorAll('form[data-sending]').forEach((f) => {
      delete f.dataset.sending
      f.removeAttribute('aria-busy')
      f.querySelectorAll('.is-sending').forEach((b) => b.classList.remove('is-sending'))
    })
  })

  if (location.hash === '#check') {   // arrived from Ctrl+U on a finished case: ready for the next slip
    history.replaceState(null, '', location.pathname + location.search)
    focusUpload({ openPicker: false })
  }

  // the server prints "Ctrl+U"-style hints hidden; show them now that the keys work, in this platform's words
  document.querySelectorAll('kbd[data-keys]').forEach((k) => {
    k.textContent = k.dataset.keys.replace('Mod', modLabel)
    k.hidden = false
  })
}
