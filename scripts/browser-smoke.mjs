import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const root = process.env.SMOKE_BASE ?? 'http://localhost:18080'
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ['--no-sandbox'],
})
const credentials = process.env.SMOKE_PASSWORD ?? 'local-scenario-password'
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAwAAAAMCAIAAADZF8uwAAAAFUlEQVR4nGNkaGAgCJgIKxlVRH9FAPOMAJg3Xd9tAAAAAElFTkSuQmCC', 'base64')
const waitApi = (p, path, method, status) =>
  p.waitForResponse(r => r.url().includes(path) && r.request().method() === method && r.status() === status)
const button = (p, name) => p.getByRole('button', { name, exact: true })

async function login(username) {
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 900 } })
  const page = await context.newPage()
  await page.goto(root + '/login')
  await page.locator('#username').fill(username)
  await page.locator('#password').fill(credentials)
  const logged = waitApi(page, '/session/login', 'POST', 200)
  await button(page, 'Войти').click()
  await logged
  await page.waitForURL(username === 'admin' ? '**/admin' : username === 'reviewer' ? '**/approvals' : '**/requests')
  assert((await context.cookies()).some(c => c.name === 'nebula_session' && c.httpOnly))
  return page
}

async function submit(staff, kind) {
  const pending = waitApi(staff, '/' + kind + '/submit', 'POST', 201)
  await button(staff, 'Отправить на проверку').click()
  return (await (await pending).json()).id
}

async function decide(reviewer, id, reject) {
  await reviewer.goto(root + '/approvals/' + id)
  if (reject) {
    await button(reviewer, 'Вернуть на доработку').click()
    await reviewer.locator('#comment').fill('Уточнить приёмку')
  }
  const decided = waitApi(reviewer, '/approvals/' + id + '/', 'POST', 200)
  await button(reviewer, reject ? 'Отправить на доработку' : 'Утвердить').click()
  await decided
}

try {
  const admin = await login("admin")
  await button(admin, "Добавить пользователя").click()
  await admin.getByLabel("Логин", { exact: true }).fill("browser-multi")
  await admin.getByLabel("Имя", { exact: true }).fill("Три роли в одном аккаунте")
  await admin.getByLabel("Адрес жителя").fill("Учебный адрес, 1")
  for (const label of ["Житель", "Сотрудник УК", "Проверяющий"]) await admin.getByRole("checkbox", { name: label }).check()
  await admin.getByLabel("Пароль", { exact: true }).fill(credentials)
  const addedAccount = waitApi(admin, "/admin/users", "POST", 201)
  await button(admin, "Сохранить").click()
  assert((await (await addedAccount).json()).roles.length === 3)
  await button(admin, "Шаблоны").click()
  await admin.route("**/api/v1/admin/templates/import", route => route.fulfill({
    json: { fields: { letterSubject: "Импортированный шаблон", letterBody: "Учебный черновик." } },
  }))
  await admin.getByLabel("Файл шаблона").setInputFiles({
    name: "template.txt", mimeType: "text/plain", buffer: Buffer.from("Учебный шаблон"),
  })
  await button(admin, "Распознать шаблон").click()
  await admin.getByText("Черновик подставлен в поля.", { exact: false }).waitFor()
  assert.equal(await admin.getByLabel("Тема письма", { exact: true }).inputValue(), "Импортированный шаблон")
  const subject = admin.getByLabel("Тема письма", { exact: true })
  await subject.fill("Браузерная проверка {{draft_subject}}")
  await button(admin, "Сохранить шаблоны").click()
  await admin.getByRole("status").getByText("Сохранено").waitFor()
  await button(admin, "Подставить стандартные тексты").click()
  await button(admin, "Сохранить шаблоны").click()
  await admin.getByRole("status").getByText("Сохранено").waitFor()
  await button(admin, "Аналитика").click()
  await admin.getByText("Использование модели", { exact: true }).waitFor()
  const multi = await login("browser-multi")
  await button(multi, "Житель").click()
  const switched = waitApi(multi, "/session/role", "PUT", 200)
  await button(multi, "Проверяющий").click()
  await switched
  await multi.waitForURL("**/approvals")
  await multi.getByRole("heading", { name: "Очередь на проверку" }).first().waitFor()
  const resident = await login('resident')
  const staff = await login('staff')
  const reviewer = await login('reviewer')
  await resident.goto(root + "/profile?next=request")
  await resident.getByLabel("ФИО", { exact: true }).fill("Житель Браузерный Тест")
  await resident.getByLabel("Полный адрес").fill("Екатеринбург, улица Тестовая, дом 1")
  await resident.getByLabel("Подъезд", { exact: true }).fill("3")
  await resident.getByLabel("Этаж", { exact: true }).fill("4")
  await resident.getByLabel("Квартира", { exact: true }).fill("12")
  await button(resident, "Сохранить профиль").click()
  await resident.waitForURL("**/requests/new")
  await resident.goto(root + '/requests/new')
  assert.equal(await resident.locator("#entrance").inputValue(), "3")
  assert.equal(await resident.getByLabel("Этаж", { exact: true }).inputValue(), "4")
  assert.equal(await resident.getByLabel("Квартира", { exact: true }).inputValue(), "12")
  await resident.locator("#entrance").fill("9")
  await button(resident, "Подставить из профиля").click()
  assert.equal(await resident.locator("#entrance").inputValue(), "3")
  await resident.route("**/api/v1/requests/description-draft", route => route.fulfill({
    json: { description: "На потолке видны следы протечки." },
  }))
  await resident.getByTestId('photos').setInputFiles({ name: 'roof.png', mimeType: 'image/png', buffer: png })
  await resident.waitForFunction(() => document.querySelector("#description")?.value === "На потолке видны следы протечки.")
  await resident.locator('#description').fill('Протечка крыши, браузерный сценарий')
  await resident.locator('#entrance').fill('2')
  const created = waitApi(resident, '/api/v1/requests', 'POST', 201)
  await button(resident, 'Отправить заявку').click()
  const request = await (await created).json()
  assert(request.description === "Протечка крыши, браузерный сценарий")
  assert(request.residentName === "Житель Браузерный Тест" && request.apartment === "12")
  const path = root + '/requests/' + request.id

  await staff.goto(path)
  await staff.getByLabel('Тип работ').fill('Ремонт кровли')
  await staff.getByLabel('Срочность').selectOption('EMERGENCY')
  await staff.getByLabel('Маршрут').selectOption('EMERGENCY_CONTRACTOR_CALL')
  const confirmed = waitApi(staff, '/triage/confirm', 'POST', 200)
  await button(staff, 'Подтвердить маршрут').click()
  await confirmed
  await staff.locator('[data-step="TZ"]').click()

  async function editTz(text) {
    const fields = staff.locator('details')
    await fields.first().waitFor()
    for (const field of await fields.all()) {
      if (await field.getAttribute('open') === null) await field.locator('summary').click()
      await field.locator('textarea').fill(text)
    }
    const saved = waitApi(staff, '/tz', 'PUT', 200)
    await button(staff, 'Сохранить').click()
    await saved
  }

  await editTz('Ремонт кровли. 30 м², 10 дней, приёмка по акту.')
  await decide(reviewer, await submit(staff, 'tz'), true)
  await editTz('Ремонт кровли. 30 м², 10 дней, приёмка с проверкой герметичности.')
  await decide(reviewer, await submit(staff, 'tz'), false)

  await staff.locator('[data-step="QUOTES_NMC"]').click()
  await button(staff, 'Подготовить письмо').click()
  await staff.getByTestId('letter').waitFor()
  for (let i = 0; i < 3; i++) {
    await staff.getByTestId('quote-files').setInputFiles({
      name: 'quote-' + i + '.txt', mimeType: 'text/plain', buffer: Buffer.from('Предложение'),
    })
    const added = waitApi(staff, '/quotes', 'POST', 202)
    await button(staff, 'Добавить КП').click()
    const q = await (await added).json()
    const quote = staff.getByTestId('quote').filter({ hasText: 'quote-' + i + '.txt' })
    await quote.getByRole('button', { name: 'Проверить или заполнить поля' }).click()
    await quote.getByLabel('Подрядчик', { exact: true }).fill('Участник ' + i)
    await quote.getByLabel('Итоговая цена').fill(String(100 + 10 * i))
    const saved = waitApi(staff, '/quotes/' + q.id + '/data', 'PUT', 200)
    await button(staff, 'Сохранить').click()
    await saved
  }
  await button(staff, 'Рассчитать').click()
  assert((await staff.getByTestId('nmc-amount').textContent()).includes('110'))
  await decide(reviewer, await submit(staff, 'nmc'), false)

  await staff.locator('[data-step="TENDER"]').click()
  await staff.getByRole('radio').first().check()
  const winner = waitApi(staff, '/tender/winner', 'PUT', 200)
  await button(staff, 'Выбрать').click()
  await winner
  await staff.locator('[data-step="PACKAGE"]').click()
  await decide(reviewer, await submit(staff, 'package'), false)
  await staff.getByTestId('package-approved').waitFor()
  const zip = staff.waitForEvent('download')
  await button(staff, 'Скачать всё').click()
  assert((await zip).suggestedFilename().endsWith('.zip'))
  const document = staff.waitForEvent('download')
  await staff.getByRole('button', { name: 'Скачать Техническое задание.docx' }).click()
  assert((await document).suggestedFilename().endsWith('.docx'))
  await resident.goto(path)
  await resident.getByText('Выполнена', { exact: true }).waitFor()
  function session(page) {
    return page.evaluate(async () => (await fetch("/api/v1/session")).json())
  }
  async function api(page, method, endpoint, options = {}) {
    const sessionData = await session(page)
    const res = await page.request.fetch(root + "/api/v1" + endpoint, { ...options, method,
      headers: { "X-CSRF-Token": sessionData.csrfToken, ...options.headers } })
    assert(res.ok(), endpoint + ": " + (await res.text()))
    return res.json()
  }
  const photo = await api(resident, "POST", "/files", { multipart: { file: { name: "auto.png", mimeType: "image/png", buffer: png } } })
  const automaticRequest = await api(resident, "POST", "/requests", { headers: { "Idempotency-Key": "browser-auto-" + Date.now() },
    data: { description: "Проверка автоматической сборки пакета", entrance: "1", photoFileIds: [photo.fileId] } })
  const autoPath = "/requests/" + automaticRequest.id
  await staff.goto(root + autoPath)
  const enabled = waitApi(staff, autoPath + "/automation", "POST", 200)
  await button(staff, "Включить автоматическую подготовку").click()
  await enabled
  await api(staff, "POST", autoPath + "/triage/confirm", { data: { urgency: "PLANNED", route: "PLANNED_PROCUREMENT", workType: "Ремонт кровли" } })
  const draftTz = await api(staff, "GET", autoPath + "/tz")
  await api(staff, "PUT", autoPath + "/tz", { data: { baseVersion: draftTz.version,
    sections: draftTz.sections.map(s => ({ id: s.id, text: "Восстановить кровлю и проверить отсутствие протечек." })) } })
  for (let i = 0; i < 3; i++) {
    const f = await api(staff, "POST", "/files", { multipart: { file: { name: "auto-kp.txt", mimeType: "text/plain", buffer: Buffer.from("Offer") } } })
    const q = await api(staff, "POST", autoPath + "/quotes", { data: { fileIds: [f.fileId] } })
    await api(staff, "PUT", autoPath + "/quotes/" + q.id + "/data", { data: { baseVersion: q.version,
      data: { contractorName: "Автоматический тест " + i, totalPrice: { amount: String(100 + i * 10), currency: "RUB" } } } })
  }
  await staff.goto(root + autoPath + "?step=PACKAGE")
  await button(staff, "Скачать черновик").waitFor()
  const automaticApproval = (await api(reviewer, "GET", "/approvals")).items.find(a => a.requestId === automaticRequest.id)
  await reviewer.goto(root + "/approvals/" + automaticApproval.id)
  await reviewer.getByText("Техническое задание", { exact: true }).first().waitFor()
  await reviewer.getByText("Расчёт НМЦ", { exact: true }).first().waitFor()
  await decide(reviewer, automaticApproval.id, false)
  await staff.getByTestId("package-approved").waitFor()
  console.log('Nuxt browser scenario passed')
} finally {
  await browser.close()
}
