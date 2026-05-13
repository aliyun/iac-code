from iac_code.tasks.notification_queue import NotificationQueue


class TestNotificationQueue:
    def test_enqueue_dequeue(self):
        q = NotificationQueue()
        q.enqueue(task_id="t1", message="Done")
        assert q.has_pending()
        n = q.dequeue()
        assert n.task_id == "t1"
        assert not q.has_pending()

    def test_fifo(self):
        q = NotificationQueue()
        q.enqueue(task_id="t1", message="First")
        q.enqueue(task_id="t2", message="Second")
        assert q.dequeue().task_id == "t1"
        assert q.dequeue().task_id == "t2"

    def test_empty(self):
        q = NotificationQueue()
        assert q.dequeue() is None

    def test_max_pending(self):
        q = NotificationQueue(max_pending=2)
        q.enqueue(task_id="t1", message="1")
        q.enqueue(task_id="t2", message="2")
        q.enqueue(task_id="t3", message="3")
        assert q.dequeue().task_id == "t2"
