// Fewer clicks at the counter: a photo starts its check as soon as it is chosen, dropped anywhere on the page,
// or pasted with Ctrl/Cmd+V (for example an image copied from WhatsApp Web). Works only where the page offers
// a photo form (form[data-primary] with input[data-shortcut="upload"]). Hold outcomes are never automated.

// a photo, or a PDF prescription (each page is read on the server)
const isImage = (f) => f && (/^image\//.test(f.type) || f.type === 'application/pdf')

export function installQuickCheck() {
  const inputs = [...document.querySelectorAll('form[data-primary] input[type="file"][data-shortcut="upload"]')]
  if (!inputs.length) return
  const root = document.documentElement
  const target = () => inputs.find((i) => i.getClientRects().length > 0) || inputs[0]

  // Double submits are stopped by the submit listener in shortcuts.js (form.dataset.sending), which also
  // runs on requestSubmit(): setting that flag here would make it cancel this very submit.
  function start(input) {
    const form = input.form
    if (!form || form.dataset.sending) return
    const btn = form.querySelector('button[type="submit"]')
    if (btn) {
      btn.textContent = 'Analysing…'
      btn.classList.add('is-sending')
    }
    // one action, and it says what it is doing: the page changes when the image check is done
    const status = form.querySelector('[data-upload-status]')
    if (status) {
      const f = input.files && input.files[0]
      status.textContent = `Uploading ${f ? f.name : 'the file'} and checking the image (about 2 s)…`
    }
    if (form.requestSubmit) form.requestSubmit()
    else form.submit()
  }

  function useFile(file) {
    if (!isImage(file)) return false
    const input = target()
    const dt = new DataTransfer()
    dt.items.add(file)
    input.files = dt.files
    start(input)
    return true
  }

  inputs.forEach((i) => i.addEventListener('change', () => {
    if (i.files.length === 1 && isImage(i.files[0])) start(i)
  }))

  document.addEventListener('paste', (e) => {
    const t = e.target
    if (t && (t.isContentEditable || /^(TEXTAREA|SELECT)$/.test(t.tagName) || (t.tagName === 'INPUT' && t.type !== 'file'))) return
    const file = [...((e.clipboardData && e.clipboardData.files) || [])].find(isImage)
    if (file && useFile(file)) e.preventDefault()
  })

  document.addEventListener('dragover', (e) => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return
    e.preventDefault()
    root.classList.add('rx-drag')
  })
  document.addEventListener('dragleave', (e) => { if (!e.relatedTarget) root.classList.remove('rx-drag') })
  document.addEventListener('drop', (e) => {
    root.classList.remove('rx-drag')
    if (inputs.includes(e.target)) return            // dropped on the field itself: its 'change' starts the check
    const file = [...((e.dataTransfer && e.dataTransfer.files) || [])].find(isImage)
    if (!file) return
    e.preventDefault()
    useFile(file)
  })
}
