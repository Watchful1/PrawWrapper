import praw
import prawcore
import traceback
import requests
import logging.handlers
import os
import configparser
from datetime import timedelta
from datetime import datetime
from enum import Enum
import re
import time


log = logging.getLogger("bot")


def id_from_fullname(fullname):
	return re.sub(r't\d_', "", fullname)


class ReturnType(Enum):
	SUCCESS = 1
	INVALID_USER = 2
	USER_DOESNT_EXIST = 3
	FORBIDDEN = 4
	THREAD_LOCKED = 5
	DELETED_COMMENT = 6
	QUARANTINED = 7
	RATELIMIT = 8
	THREAD_REPLIED = 9
	NOTHING_RETURNED = 10
	SUBREDDIT_NOT_ENABLED = 11
	SUBMISSION_NOT_PROCESSED = 12
	NOT_WHITELISTED_BY_USER_MESSAGE = 13
	PM_MODERATOR_RESTRICTION = 14
	SUBREDDIT_OUTBOUND_LINKING_DISALLOWED = 15
	SUBREDDIT_LINKING_DISALLOWED = 16
	COMMENT_UNREPLIABLE = 17
	SOMETHING_IS_BROKEN = 18


class PushshiftType(Enum):
	PROD = 1
	BETA = 2
	AUTO = 3


class Queue:
	def __init__(self, max_size):
		self.list = []
		self.max_size = max_size
		self.set = set()

	def put(self, item):
		if len(self.list) >= self.max_size:
			removed_item = self.list.pop(0)
			if removed_item not in self.set:
				log.warning(f"{removed_item} not in set when removing")
			else:
				self.set.remove(removed_item)
		self.list.append(item)
		self.set.add(item)

	def contains(self, item):
		return item in self.set


def get_config():
	config = configparser.ConfigParser()
	if 'APPDATA' in os.environ:  # Windows
		os_config_path = os.environ['APPDATA']
	elif 'XDG_CONFIG_HOME' in os.environ:  # Modern Linux
		os_config_path = os.environ['XDG_CONFIG_HOME']
	elif 'HOME' in os.environ:  # Legacy Linux
		os_config_path = os.path.join(os.environ['HOME'], '.config')
	else:
		raise FileNotFoundError("Could not find config")
	os_config_path = os.path.join(os_config_path, 'praw.ini')
	config.read(os_config_path)

	return config


def get_config_var(config, section, variable):
	if section not in config:
		raise ValueError(f"Section {section} not in config")

	if variable not in config[section]:
		raise ValueError(f"Variable {variable} not in section {section}")

	return config[section][variable]


class PushshiftClient:
	def __init__(self, base_url, limit_keyword, before_keyword, after_keyword, client_type, max_limit=1000, lag_keyword=None, debug=False):
		self.base_url = base_url
		self.limit_keyword = limit_keyword
		self.before_keyword = before_keyword
		self.after_keyword = after_keyword
		self.max_limit = max_limit
		self.client_type = client_type
		self.lag_keyword = lag_keyword
		self.latest = None
		self.lag_checked = None
		self.failures = 0
		self.failures_threshold = 5
		self.request_seconds = None
		self.debug = debug

	def __str__(self):
		return f"Pushshift client: {self.client_type}"

	def failed(self):
		return self.failures > 0

	def get_url(self, keyword, limit, before, after_seconds=3600):
		params = []
		if keyword is not None:
			params.append(f"q={keyword}")
		if limit is not None:
			params.append(f"{self.limit_keyword}={min(limit, self.max_limit)}")
		if before is not None:
			params.append(f"{self.before_keyword}={before}")
		elif after_seconds is not None:
			params.append(f"{self.after_keyword}={after_seconds}s")

		return self.base_url + "?" + '&'.join(params)

	def get_comments(self, keyword, limit, before, user_agent, timeout=10):
		url = self.get_url(keyword, limit, before)
		try:
			if self.debug:
				log.info(f"pushshift client: calling {url}")
			json = requests.get(url, headers={'User-Agent': user_agent}, timeout=timeout)
			if json.status_code == 200:
				self.failures = 0
				self.failures_threshold = 5
				if self.debug:
					log.info(f"pushshift client: call success")
				return json.json()['data'], None
			else:
				self.failures += 1
				if self.debug:
					log.info(f"pushshift client: call failure {json.status_code} : {self.failures}")
				return None, f"Pushshift bad status: {json.status_code}"
		except Exception as err:
			self.failures += 1
			if isinstance(err, requests.exceptions.ReadTimeout):
				if self.debug:
					log.info(f"pushshift client: call failure readtimeout : {self.failures}")
				return None, f"Pushshift read timeout"
			else:
				if self.debug:
					log.info(f"pushshift client: call failure {type(err).__name__} : {self.failures}")
				return None, f"Pushshift parse exception: {type(err).__name__} : {err}"

	def check_lag(self, user_agent):
		start_time = time.perf_counter()
		comments, result_message = self.get_comments(self.lag_keyword, 1, None, user_agent, timeout=30)
		if comments is None or len(comments) == 0:
			log.info(f"Failed to get pushshift {self.client_type} lag")
			self.request_seconds = 10
			self.lag_checked = datetime.utcnow()
			if self.latest is None:
				self.latest = datetime.utcnow()
		else:
			self.request_seconds = round(time.perf_counter() - start_time, 2)
			self.latest = datetime.utcfromtimestamp(comments[0]['created_utc'])
			self.lag_checked = datetime.utcnow()

	def lag_seconds(self):
		if self.lag_checked is None or self.latest is None:
			return 0
		return max(int(round((self.lag_checked - self.latest).total_seconds(), 0)), 0)

	def lag_minutes(self):
		return int(self.lag_seconds() / 60)


class Reddit:
	def __init__(self, user_name, no_post=False, prefix=None, user_agent=None, pushshift_client=PushshiftType.PROD, init_pushshift_lag=False, debug=False):
		log.info(f"Initializing reddit class: user={user_name} prefix={prefix} no_post={no_post}")
		self.no_post = no_post

		config = get_config()
		if prefix is None:
			prefix = ''
		else:
			prefix = prefix + "_"
		client_id = get_config_var(config, user_name, f"{prefix}client_id")
		client_secret = get_config_var(config, user_name, f"{prefix}client_secret")
		refresh_token = get_config_var(config, user_name, f"{prefix}refresh_token")
		if user_agent is None:
			self.reddit = praw.Reddit(
				user_name,
				client_id=client_id,
				client_secret=client_secret,
				refresh_token=refresh_token)
		else:
			self.reddit = praw.Reddit(
				user_name,
				client_id=client_id,
				client_secret=client_secret,
				refresh_token=refresh_token,
				user_agent=user_agent)

		self.username = self.reddit.user.me().name

		log.info(f"Logged into reddit as u/{self.username} {prefix}: {pushshift_client}")

		if user_agent is None:
			self.user_agent = self.reddit.config.user_agent
		else:
			self.user_agent = user_agent

		self.processed_comments = Queue(100)

		self.pushshift_client_type = pushshift_client
		self.recent_pushshift_client = None

		self.pushshift_prod_client = PushshiftClient(
			"https://api.pushshift.io/reddit/search/comment", "limit", "before", "after", PushshiftType.PROD, max_limit=1000, lag_keyword="*", debug=debug)
		self.pushshift_beta_client = PushshiftClient(
			"https://api.pushshift.io/reddit/search/comment", "size", "until", "since", PushshiftType.BETA, max_limit=1000, lag_keyword="*", debug=debug)

		if init_pushshift_lag:
			self.check_pushshift_lag(True)

	def run_function(self, function, arguments):
		output = None
		result = None
		try:
			output = function(*arguments)
		except praw.exceptions.APIException as err:
			for return_type in ReturnType:
				if err.error_type == return_type.name:
					result = return_type
					break
			if result is None:
				raise
		except prawcore.exceptions.Forbidden:
			result = ReturnType.FORBIDDEN
		except IndexError:
			result = ReturnType.QUARANTINED

		if result is None:
			result = ReturnType.SUCCESS
		return output, result

	def is_message(self, item):
		return isinstance(item, praw.models.Message)

	def get_messages(self, count=500):
		log.debug("Fetching unread messages")
		message_list = []
		for message in self.reddit.inbox.unread(limit=count):
			message_list.append(message)
		return message_list

	def reply_message(self, message, body):
		log.debug(f"Replying to message: {message.id}")
		if self.no_post:
			log.info(body)
			return ReturnType.SUCCESS
		else:
			output, result = self.run_function(message.reply, [body])
			return result

	def mark_read(self, message):
		log.debug(f"Marking message as read: {message.id}")
		if not self.no_post:
			message.mark_read()

	def get_submission(self, submission_id):
		log.debug(f"Fetching submission by id: {submission_id}")
		if submission_id == "xxxxxx":
			return None
		else:
			return self.reddit.submission(submission_id)

	def get_comment(self, comment_id):
		log.debug(f"Fetching comment by id: {comment_id}")
		if comment_id == "xxxxxx":
			return None
		else:
			return self.reddit.comment(comment_id)

	def subreddit_exists(self, subreddit_name):
		log.debug(f"Checking if subreddit exists: {subreddit_name}")
		reddit_subreddit = self.reddit.subreddit(subreddit_name)
		try:
			reddit_subreddit._fetch()
		except (prawcore.exceptions.Redirect, prawcore.exceptions.NotFound):
			return False
		except prawcore.exceptions.Forbidden:
			return False
		except prawcore.exceptions.BadRequest:
			return False
		return True

	def redditor_exists(self, redditor_name):
		log.debug(f"Checking if redditor exists: {redditor_name}")
		redditor = self.reddit.redditor(redditor_name)
		try:
			redditor._fetch()
		except prawcore.exceptions.NotFound:
			return False
		return True

	def edit_comment(self, body, comment=None, comment_id=None):
		if comment is None:
			comment = self.get_comment(comment_id)
		log.debug(f"Editing comment: {comment.id}")

		if self.no_post:
			log.info(body)
		else:
			output, result = self.run_function(comment.edit, [body])
			return result

	def delete_comment(self, comment):
		log.debug(f"Deleting comment: {comment.id}")
		if not self.no_post:
			try:
				comment.delete()
			except Exception:
				log.warning(f"Error deleting comment: {comment.comment_id}")
				log.warning(traceback.format_exc())
				return False
		return True

	def send_message(self, user_name, subject, body):
		log.debug(f"Sending message to u/{user_name}")
		if self.no_post:
			log.info(body)
			return ReturnType.SUCCESS
		else:
			redditor = self.reddit.redditor(user_name)
			output, result = self.run_function(redditor.message, [subject, body])
			return result

	def reply(self, comment_submission, body):
		if self.no_post:
			log.info(body)
			return "xxxxxx", ReturnType.SUCCESS
		else:
			output, result = self.run_function(comment_submission.reply, [body])
			if output is not None:
				return output.id, result
			else:
				if result == ReturnType.SUCCESS:
					return None, ReturnType.NOTHING_RETURNED
				else:
					return None, result

	def reply_comment(self, comment, body):
		log.debug(f"Replying to comment: {comment.id}")
		return self.reply(comment, body)

	def reply_submission(self, submission, body):
		log.debug(f"Replying to submission: {submission.id}")
		return self.reply(submission, body)

	def get_subreddit_submissions(self, subreddit_name):
		log.debug(f"Getting subreddit submissions: {subreddit_name}")
		return self.reddit.subreddit(subreddit_name).new(limit=1000)

	def quarantine_opt_in(self, subreddit_name):
		log.debug(f"Opting in to subreddit: {subreddit_name}")
		if not self.no_post:
			try:
				self.reddit.subreddit(subreddit_name).quaran.opt_in()
			except Exception:
				log.warning(f"Error opting in to subreddit: {subreddit_name}")
				log.warning(traceback.format_exc())
				return False
		return True

	def get_user_creation_date(self, user_name):
		log.debug(f"Getting user creation date: {user_name}")
		try:
			return self.reddit.redditor(user_name).created_utc
		except Exception:
			return None

	def call_info(self, fullnames):
		log.debug(f"Fetching {len(fullnames)} ids from info")
		return self.reddit.info(fullnames)

	def get_active_clients(self):
		if self.pushshift_client_type == PushshiftType.PROD:
			return [self.pushshift_prod_client]
		elif self.pushshift_client_type == PushshiftType.BETA:
			return [self.pushshift_beta_client]
		else:
			return [self.pushshift_prod_client, self.pushshift_beta_client]

	def check_pushshift_lag(self, force=False):
		for client in self.get_active_clients():
			if force or client.lag_checked is None or datetime.utcnow() - timedelta(minutes=2) > client.lag_checked:
				client.check_lag(self.user_agent)

	def get_effective_pushshift_lag(self):
		if self.recent_pushshift_client is None:
			return 0
		elif self.recent_pushshift_client == PushshiftType.PROD:
			return self.pushshift_prod_client.lag_minutes()
		elif self.recent_pushshift_client == PushshiftType.BETA:
			return self.pushshift_beta_client.lag_minutes()
		else:
			return 0

	def get_pushshift_client(self):
		if self.pushshift_client_type == PushshiftType.PROD:
			return self.pushshift_prod_client
		elif self.pushshift_client_type == PushshiftType.BETA:
			return self.pushshift_beta_client
		elif self.pushshift_client_type == PushshiftType.AUTO:
			if self.pushshift_prod_client.failed() or self.pushshift_beta_client.failed():
				if self.pushshift_beta_client.lag_seconds() < 600:
					return self.pushshift_beta_client
				elif self.pushshift_prod_client.lag_seconds() < 600:
					return self.pushshift_prod_client
				elif self.pushshift_prod_client.lag_seconds() < self.pushshift_beta_client.lag_seconds():
					return self.pushshift_prod_client
				else:
					return self.pushshift_beta_client
			elif self.pushshift_beta_client.lag_seconds() < 120:
				return self.pushshift_beta_client
			elif self.pushshift_prod_client.lag_seconds() < self.pushshift_beta_client.lag_seconds():
				return self.pushshift_prod_client
			else:
				return self.pushshift_beta_client
		else:
			return self.pushshift_prod_client

	def set_recent_pushshift_client(self, new_client_type):
		if self.recent_pushshift_client is not None and self.recent_pushshift_client != new_client_type:
			log.warning(f"Switching pushshift client from {self.recent_pushshift_client} to {new_client_type}")
		self.recent_pushshift_client = new_client_type

	def get_keyword_comments(self, keyword, last_seen):
		if not len(self.processed_comments.list):
			last_seen = last_seen + timedelta(seconds=1)

		log.debug(f"Fetching comments for keyword: {keyword} : {last_seen.strftime('%Y-%m-%d %H:%M:%S')}")

		self.check_pushshift_lag(False)

		client = self.get_pushshift_client()
		self.set_recent_pushshift_client(client.client_type)
		log.debug(f"Using pushshift {client.client_type} client")

		try:
			result_comments = []
			before_timestamp = None

			while True:
				comments, result_message = client.get_comments(
					keyword,
					1000 if before_timestamp is not None else 100,
					before_timestamp,
					self.user_agent
				)

				if comments is None:
					if result_message is not None and client.failures >= client.failures_threshold:
						log.warning(f"Pushshift client error, {client.failures} : {client.client_type} : {result_message}")
						client.failures_threshold = client.failures_threshold * 2
					return []

				if not len(comments):
					log.warning(f"No comments found for search term: {keyword}")
					return []

				found_seen = False
				comment_datetime = None
				for comment in comments:
					comment_datetime = datetime.utcfromtimestamp(comment['created_utc'])
					before_timestamp = comment['created_utc']
					if last_seen > comment_datetime:
						found_seen = True
						break

					if not self.processed_comments.contains(comment['id']):
						result_comments.append(comment)

				if found_seen:
					if len(result_comments) > 100:
						log.warning(
							f"Found {len(result_comments)} from {comment_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
							f" to {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
					break
				else:
					log.warning(
						f"Hit end of comments search: {last_seen.strftime('%Y-%m-%d %H:%M:%S')} : "
						f"{comment_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

			log.debug(f"Found comments: {len(result_comments)}")
			return result_comments

		except Exception as err:
			log.warning(f"Uncaught pushshift error: {err}")
			log.warning(traceback.format_exc())
			return []

	def mark_keyword_comment_processed(self, comment_id):
		self.processed_comments.put(comment_id)
