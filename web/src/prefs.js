// Display preferences for this counter PC: text size and contrast.
// Kept in a cookie (not localStorage) because the server reads it and renders <html data-text data-contrast>,
// so the very first paint already has the right size and contrast - no flash on every page load.
// Format "t<0-3>-<auto|std|hc>"; counter_app.py parses exactly this.
import { reactive, watch } from 'vue'

export const TEXT_SIZES = [100, 115, 130, 150]   // percent; matches html[data-text] in counter_app.CSS
const COOKIE = 'rxui'

function read() {
  const m = document.cookie.match(/(?:^|;\s*)rxui=t([0-3])-(auto|std|hc)/)
  return m ? { text: +m[1], contrast: m[2] } : { text: 0, contrast: 'auto' }
}

export const prefs = reactive(read())

function apply() {
  const html = document.documentElement
  html.dataset.text = String(prefs.text)
  if (prefs.contrast === 'auto') delete html.dataset.contrast   // follow the OS "increase contrast" setting
  else html.dataset.contrast = prefs.contrast === 'hc' ? 'high' : 'std'
  document.cookie = `${COOKIE}=t${prefs.text}-${prefs.contrast}; Path=/; Max-Age=31536000; SameSite=Strict`
}

watch(prefs, apply)

export function osWantsMoreContrast() {
  return window.matchMedia && window.matchMedia('(prefers-contrast: more)').matches
}
