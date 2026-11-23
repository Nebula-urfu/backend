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
  await page.waitForURL(username === 'reviewer' ? '**/approvals' : '**/requests')
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
  const resident = await login('resident')
  const staff = await login('staff')
  const reviewer = await login('reviewer')
  await resident.goto(root + '/requests/new')
  await resident.getByTestId('photos').setInputFiles({ name: 'roof.png', mimeType: 'image/png', buffer: png })
  await resident.locator('#description').fill('Протечка крыши, браузерный сценарий')
  await resident.locator('#entrance').fill('2')
  const created = waitApi(resident, '/api/v1/requests', 'POST', 201)
  await button(resident, 'Отправить заявку').click()
  const request = await (await created).json()
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
  console.log('Nuxt browser scenario passed')
} finally {
  await browser.close()
}
