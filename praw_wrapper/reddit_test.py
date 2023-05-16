import logging.handlers
import random
from datetime import datetime, timezone

from praw_wrapper.reddit import ReturnType, id_from_fullname
from praw_wrapper.ingest import IngestComment

log = logging.getLogger("bot")


def random_id():
	values = list(map(chr, range(97, 123)))
	for num in range(1, 10):
		values.append(str(num))
	return ''.join(random.choices(values, k=6))


class User:
	def __init__(self, name, created_utc=None):
		self.name = name
		self.created_utc = created_utc


class Subreddit:
	def __init__(self, name):
		self.display_name = name
		self.posts = []
		self.is_banned = False


class RedditObject:
	def __init__(
		self,
		body=None,
		author=None,
		created=None,
		id=None,
		permalink=None,
		link_id=None,
		prefix="t4",
		subreddit=None,
		dest=None,
		flair=None,
		title=None
	):
		self.body = body
		if isinstance(author, User):
			self.author = author
		else:
			self.author = User(author)
		if isinstance(dest, User):
			self.dest = dest
		else:
			self.dest = User(dest)
		if subreddit is None:
			self.subreddit = None
		elif isinstance(subreddit, Subreddit):
			self.subreddit = subreddit
		else:
			self.subreddit = Subreddit(subreddit)
		if id is None:
			self.id = random_id()
		else:
			self.id = id
		self.fullname = f"{prefix}_{self.id}"
		if created is None:
			self.created_utc = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
		else:
			self.created_utc = created.replace(tzinfo=timezone.utc).timestamp()
		if permalink is None and self.subreddit is not None:
			permalink = f"/r/{self.subreddit.display_name}/comments/{self.id}"

		if permalink is not None:
			self.permalink = permalink
			self.url = "http://www.reddit.com"+permalink
		self.link_id = link_id
		self.link_flair_text = flair

		self.parent = None
		self.children = []
		self.title = title
		self.removed_by_category = None

	def get_ingest_comment(self):
		return IngestComment(
			id=self.id,
			author=self.author.name,
			subreddit=self.subreddit.display_name,
			created_utc=self.created_utc,
			permalink=self.permalink,
			link_id=self.link_id,
			body=self.body,
			client_id=1,
		)

	def get_first_child(self):
		if len(self.children):
			return self.children[0]
		else:
			return None

	def get_last_child(self):
		if len(self.children):
			return self.children[-1]
		else:
			return None

	def mark_read(self):
		return

	def reply(self, body, author):
		new_message = RedditObject(body, author)
		new_message.parent = self
		self.children.append(new_message)
		return new_message

	def set_title(self, new_title):
		if self.title is None:
			self.title = new_title

	def set_removed_by_category(self, removed_by_category):
		if self.removed_by_category is None:
			self.removed_by_category = removed_by_category


class Reddit:
	def __init__(self, user):
		self.username = user
		self.sent_messages = []
		self.self_comments = []
		self.all_comments = {}
		self.all_submissions = {}
		self.users = {}
		self.locked_threads = set()
		self.pushshift_lag = 0
		self.subreddits = {}

	def add_comment(self, comment, self_comment=False):
		self.all_comments[comment.id] = comment
		if self_comment:
			self.self_comments.append(comment)

	def add_user(self, user):
		self.users[user.name] = user

	def add_submission(self, submission):
		self.all_submissions[submission.id] = submission

	def reply_message(self, message, body):
		self.sent_messages.append(message.reply(body, self.username))
		return ReturnType.SUCCESS

	def reply_comment(self, comment, body):
		if comment.subreddit is not None and comment.subreddit.display_name in self.subreddits and \
				self.subreddits[comment.subreddit.display_name].is_banned:
			return None, ReturnType.FORBIDDEN
		elif comment.link_id is not None and id_from_fullname(comment.link_id) in self.locked_threads:
			return None, ReturnType.THREAD_LOCKED
		elif comment.id not in self.all_comments:
			return None, ReturnType.DELETED_COMMENT
		else:
			new_comment = comment.reply(body, self.username)
			self.add_comment(new_comment, True)
			return new_comment.id, ReturnType.SUCCESS

	def reply_submission(self, submission, body):
		new_comment = submission.reply(body, self.username)
		self.add_comment(new_comment, True)
		return new_comment.id, ReturnType.SUCCESS

	def mark_read(self, message):
		message.mark_read()

	def send_message(self, username, subject, body):
		new_message = RedditObject(body, self.username, dest=username)
		self.sent_messages.append(new_message)
		return ReturnType.SUCCESS

	def subreddit_exists(self, subreddit_name):
		# not worth testing without the reddit api
		return True

	def redditor_exists(self, redditor_name):
		# not worth testing without the reddit api
		return True

	def get_comment(self, comment_id):
		if comment_id in self.all_comments:
			return self.all_comments[comment_id]
		else:
			return RedditObject(id=comment_id)

	def get_submission(self, submission_id):
		if submission_id in self.all_submissions:
			return self.all_submissions[submission_id]
		else:
			return None

	def edit_comment(self, body, comment=None, comment_id=None):
		if comment is None:
			comment = self.get_comment(comment_id)

		comment.body = body
		return ReturnType.SUCCESS

	def delete_comment(self, comment):
		if comment.id in self.all_comments:
			del self.all_comments[comment.id]
		try:
			self.self_comments.remove(comment)
		except ValueError:
			pass

		if comment.parent is not None:
			try:
				comment.parent.children.remove(comment)
			except ValueError:
				pass

		for child in comment.children:
			child.parent = None

		return True

	def add_subreddit(self, subreddit):
		self.subreddits[subreddit.display_name] = subreddit

	def ban_subreddit(self, subreddit_name):
		if subreddit_name not in self.subreddits:
			self.subreddits[subreddit_name] = Subreddit(subreddit_name)
		self.subreddits[subreddit_name].is_banned = True

	def lock_thread(self, thread_id):
		self.locked_threads.add(thread_id)

	def get_subreddit_submissions(self, subreddit_names):
		posts = []
		for subreddit_name in subreddit_names.split("+"):
			posts.extend(self.subreddits[subreddit_name].posts)

		return reversed(sorted(posts, key=lambda post: post.created_utc))

	def get_user_creation_date(self, user_name):
		if user_name in self.users:
			return self.users[user_name].created_utc
		else:
			return None

	def get_effective_pushshift_lag(self):
		return 0

	def call_info(self, fullnames):
		results = []
		for fullname in fullnames:
			if fullname[:3] == "t3_" and fullname[3:] in self.all_submissions:
				results.append(self.all_submissions[fullname[3:]])
			elif fullname[:3] == "t1_" and fullname[3:] in self.all_comments:
				results.append(self.all_comments[fullname[3:]])

		return results
