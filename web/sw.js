/* Службовий воркер: тримає застосунок працездатним, коли ноутбук вимкнений.
   Сторінку віддаємо з кеша одразу, дані — спершу з мережі, а як її немає,
   з останнього збереженого знімка. Нічого качати вручну не треба: кеш
   наповнюється сам, поки ви користуєтеся застосунком онлайн. */

const VERSION = 'coach-v1';
const SHELL = VERSION + '-shell';   // сторінка і маніфест
const DATA = VERSION + '-data';     // відповіді /api

// Що тримаємо офлайн. /api/review не кешуємо окремо: колоду збираємо
// з /api/words, інакше кожен фільтр був би окремим записом у кеші.
const DATA_PATHS = ['/api/stats', '/api/words', '/api/collections', '/api/materials'];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await cache.addAll(['/', '/manifest.json']);
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Прибираємо кеші попередніх версій, щоб не розросталися
    for (const name of await caches.keys()) {
      if (!name.startsWith(VERSION)) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

/* Сторінка: віддаємо з кеша миттєво, а свіжу тихо кладемо на наступний раз. */
async function shellFirst(request) {
  const cache = await caches.open(SHELL);
  const cached = await cache.match('/');
  const fresh = fetch(request).then((response) => {
    if (response.ok) cache.put('/', response.clone());
    return response;
  }).catch(() => null);
  return cached || (await fresh) || new Response('Offline', {status: 503});
}

/* Позначаємо відповіді, зібрані з кеша: сторінка за цим розуміє, що
   показує знімок, навіть якщо браузер ще не встиг перемкнути navigator.onLine. */
async function fromCache(response) {
  const headers = new Headers(response.headers);
  headers.set('X-Coach-Cache', '1');
  return new Response(await response.blob(), {status: 200, headers});
}

/* Дані: спершу мережа, бо вони міняються; без мережі — останній знімок. */
async function dataFirst(request, url) {
  const cache = await caches.open(DATA);
  try {
    const response = await fetch(request);
    if (response.ok && DATA_PATHS.includes(url.pathname)) {
      cache.put(url.pathname, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(url.pathname);
    if (cached) return fromCache(cached);
    if (url.pathname === '/api/review') return reviewFromCache(url, cache);
    return new Response(JSON.stringify({error: 'offline'}),
      {status: 503, headers: {'Content-Type': 'application/json'}});
  }
}

/* Колода для повторень без сервера: беремо збережений словник і повторюємо
   ту саму вибірку, що й сервер — вивчені окремо, улюблені окремо, решта
   за датою повторення. */
async function reviewFromCache(url, cache) {
  const snapshot = await cache.match('/api/words');
  if (!snapshot) {
    return new Response(JSON.stringify({error: 'offline'}),
      {status: 503, headers: {'Content-Type': 'application/json'}});
  }
  const {words} = await snapshot.json();
  const scope = url.searchParams.get('collection') || '';
  const today = new Date().toISOString().slice(0, 10);
  let deck;
  if (scope === 'favorites') {
    deck = words.filter((w) => w.favorite && !w.learned);
  } else if (scope === 'learned') {
    deck = words.filter((w) => w.learned);
  } else {
    deck = words.filter((w) => !w.learned && (!w.due || w.due <= today));
    if (scope) deck = deck.filter((w) => (w.collections || []).includes(Number(scope)));
  }
  return new Response(JSON.stringify({words: deck, offline: true}),
    {headers: {'Content-Type': 'application/json', 'X-Coach-Cache': '1'}});
}

self.addEventListener('fetch', (event) => {
  const {request} = event;
  if (request.method !== 'GET') return;            // зміни надсилає сама сторінка
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    return event.respondWith(shellFirst(request));
  }
  if (url.pathname.startsWith('/api/')) {
    return event.respondWith(dataFirst(request, url));
  }
  if (url.pathname === '/manifest.json') {
    return event.respondWith(caches.match(url.pathname).then((r) => r || fetch(request)));
  }
});
