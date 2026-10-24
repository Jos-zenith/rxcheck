// While the server reads the photo (5-15 s), the form is already usable. When the reading is done, this fills
// the fields that are still empty and swaps in the server's summary of what was read. A field staff typed in
// is never touched. Whether a filled value needs a tick is decided on the server when the form is sent, so
// nothing here can skip a confirmation.
const POLL_MS = 1500
const MAX_TRIES = 60

function fill(input, value) {
  if (input.getAttribute('role') === 'combobox') {
    // a lookup field (Vue): set its model without opening the suggestion list
    input.dispatchEvent(new CustomEvent('rx-fill', { detail: value }))
  } else {
    input.value = value
  }
}

function apply(box, data) {
  const filled = []
  for (const [name, value] of Object.entries(data.values || {})) {
    const input = document.getElementById(name)
    if (!input || !value || input.value.trim()) continue
    fill(input, value)
    filled.push(input.labels && input.labels[0] ? input.labels[0].textContent.trim() : name)
  }
  const tpl = document.createElement('template')
  tpl.innerHTML = data.panel || ''
  const panel = tpl.content.firstElementChild
  if (panel) {
    if (filled.length) {
      const p = document.createElement('p')
      p.className = 'hint'
      p.textContent = `Filled in just now: ${filled.join('; ')}.`
      panel.appendChild(p)
    }
    box.replaceWith(panel)
  } else {
    box.remove()
  }
}

export function installOcrFill() {
  const box = document.querySelector('[data-ocr-pending]')
  if (!box) return
  const url = box.dataset.ocrPending
  let tries = 0
  const tick = async () => {
    tries += 1
    let data = null
    try {
      const res = await fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
      if (res.ok) data = await res.json()
    } catch (ex) {
      data = null
    }
    if (data && data.status !== 'pending') { apply(box, data); return }
    if (tries < MAX_TRIES) { setTimeout(tick, POLL_MS); return }
    box.querySelector('p').textContent = 'Reading the photo is taking too long. Type the medicines from the slip.'
  }
  setTimeout(tick, POLL_MS)
}
