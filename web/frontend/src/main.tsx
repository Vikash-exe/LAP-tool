import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { signInAgain } from './api.ts'

// Every request goes through here: when the server says "sign in required" (the session ran out, or the user was
// removed), go to the sign-in page and come back afterwards.
const nativeFetch = window.fetch.bind(window)
window.fetch = async (...args: Parameters<typeof fetch>) => {
  const res = await nativeFetch(...args)
  if (res.status === 401) {
    const first = args[0]
    const url = typeof first === 'string' ? first : first instanceof Request ? first.url : String(first)
    if (!url.includes('/api/login')) signInAgain()
  }
  return res
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
