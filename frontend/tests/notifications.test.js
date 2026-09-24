import assert from 'node:assert/strict'
import test from 'node:test'
import { useNotifications } from '../src/composables/useNotifications.js'

test('通知支持明确类型、后续动作和手动关闭', () => {
  const notices = useNotifications()
  let acted = false
  const id = notices.success('操作完成', '权威状态已保存', {
    duration: 0,
    actionLabel: '查看结果',
    onAction: () => { acted = true },
  })
  const item = notices.notifications.find((entry) => entry.id === id)

  assert.equal(item.type, 'success')
  assert.equal(item.actionLabel, '查看结果')
  item.onAction()
  assert.equal(acted, true)
  notices.dismiss(id)
  assert.equal(notices.notifications.some((entry) => entry.id === id), false)
})

