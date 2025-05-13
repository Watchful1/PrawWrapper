import praw
import prawcore
import traceback
import logging.handlers
import os
import configparser
from datetime import datetime
from enum import Enum
import re
import prometheus_client
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
	COMMENT_GUIDANCE_VALIDATION_FAILED = 19


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


class Reddit:
	def __init__(self, user_name, no_post=False, prefix=None, user_agent=None):
		log.info(f"Initializing reddit class: user={user_name} prefix={prefix} no_post={no_post}")
		self.no_post = no_post

		self.rate_requests_remaining = prometheus_client.Gauge('rate_requests_remaining', "Number of requests remaining in the window", ['username'])
		self.rate_seconds_remaining = prometheus_client.Gauge('rate_seconds_remaining', "Number of seconds till the window reset",['username'])
		self.rate_requests_used = prometheus_client.Gauge('rate_requests_used', "Number of requests used", ['username'])
		self.ratelimit_slept = prometheus_client.Counter('ratelimit_slept', "Time slept", ['username'])

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

		log.info(f"Logged into reddit as u/{self.username} {prefix}")

		if user_agent is None:
			self.user_agent = self.reddit.config.user_agent
		else:
			self.user_agent = user_agent

		self.ratelimit_regex = re.compile(r"([0-9]{1,3}) (milliseconds?|seconds?|minutes?)")

	def record_rate_limits(self):
		# if self.counters is None:
		# 	return
		remaining = int(self.reddit._core._rate_limiter.remaining)
		used = int(self.reddit._core._rate_limiter.used)
		self.rate_requests_remaining.labels(username=self.username).set(remaining)  # rate_requests_remaining
		self.rate_requests_used.labels(username=self.username).set(used)  # rate_requests_used

		reset_timestamp = self.reddit._core._rate_limiter.reset_timestamp
		seconds_to_reset = (datetime.utcfromtimestamp(reset_timestamp) - datetime.utcnow()).total_seconds()
		self.rate_seconds_remaining.labels(username=self.username).set(int(seconds_to_reset))  # rate_seconds_remaining

	def get_ratelimit_seconds(self, err):
		for item in err.items:
			if item.error_type == "RATELIMIT":
				amount_search = self.ratelimit_regex.search(item.message)
				if not amount_search:
					break
				seconds = int(amount_search.group(1))
				if amount_search.group(2).startswith("minute"):
					seconds *= 60
				elif amount_search.group(2).startswith("millisecond"):
					seconds = 0
				return seconds + 1
		return None

	def run_function(self, function, arguments, retry_seconds=0):
		output = None
		result = None
		try:
			output = function(*arguments)
		except praw.exceptions.APIException as err:
			for return_type in ReturnType:
				if err.error_type == return_type.name:
					result = return_type
					if result == ReturnType.RATELIMIT:
						seconds = self.get_ratelimit_seconds(err)
						seconds += 10
						if seconds is not None:
							if seconds < retry_seconds:
								log.warning(f"Got a ratelimit response, sleeping {seconds}/{retry_seconds}")
								self.ratelimit_slept.labels(username=self.username).inc(seconds)
								time.sleep(seconds)
								self.run_function(function, arguments, retry_seconds - seconds)
							else:
								log.warning(f"Got a ratelimit response, but {seconds} was greater than remaining retry seconds {retry_seconds}")
						else:
							message = ""
							for item in err.items:
								if item.error_type == "RATELIMIT":
									message = item.message
							log.warning(f"Got a ratelimit response, but no seconds were found so we can't sleep, retrying once : {message}")
							self.run_function(function, arguments, 0)
					break
			if result is None:
				raise
		except prawcore.exceptions.Forbidden:
			result = ReturnType.FORBIDDEN
		except IndexError:
			result = ReturnType.QUARANTINED

		if result is None:
			result = ReturnType.SUCCESS
		self.record_rate_limits()
		return output, result

	def is_message(self, item):
		return isinstance(item, praw.models.Message)

	def get_messages(self, count=500):
		log.debug("Fetching unread messages")
		message_list = []
		for message in self.reddit.inbox.unread(limit=count):
			message_list.append(message)
		self.record_rate_limits()
		return message_list

	def reply_message(self, message, body, retry_seconds=0):
		log.debug(f"Replying to message: {message.id}")
		if self.no_post:
			log.info(body)
			return ReturnType.SUCCESS
		else:
			output, result = self.run_function(message.reply, [body], retry_seconds)
			return result

	def mark_read(self, message):
		log.debug(f"Marking message as read: {message.id}")
		if not self.no_post:
			message.mark_read()
		self.record_rate_limits()

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
			self.record_rate_limits()
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
			self.record_rate_limits()
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
				self.record_rate_limits()
			except Exception:
				log.warning(f"Error deleting comment: {comment.comment_id}")
				log.warning(traceback.format_exc())
				return False
		return True

	def send_message(self, user_name, subject, body, retry_seconds=0):
		log.debug(f"Sending message to u/{user_name}")
		if user_name in {'[deleted]'}:
			log.warning(f"Trying to send message to u/{user_name}, skipping")
			return ReturnType.INVALID_USER
		if self.no_post:
			log.info(body)
			return ReturnType.SUCCESS
		else:
			redditor = self.reddit.redditor(user_name)
			output, result = self.run_function(redditor.message, [subject, body], retry_seconds)
			return result

	def reply(self, comment_submission, body, retry_seconds=0):
		if self.no_post:
			log.info(body)
			return "xxxxxx", ReturnType.SUCCESS
		else:
			output, result = self.run_function(comment_submission.reply, [body], retry_seconds)
			if output is not None:
				return output.id, result
			else:
				if result == ReturnType.SUCCESS:
					return None, ReturnType.NOTHING_RETURNED
				else:
					return None, result

	def reply_comment(self, comment, body):
		log.debug(f"Replying to comment: {comment.id}")
		result = self.reply(comment, body)
		self.record_rate_limits()
		return result

	def reply_submission(self, submission, body):
		log.debug(f"Replying to submission: {submission.id}")
		result = self.reply(submission, body)
		self.record_rate_limits()
		return result

	def get_subreddit_submissions(self, subreddit_name):
		log.debug(f"Getting subreddit submissions: {subreddit_name}")
		return self.reddit.subreddit(subreddit_name).new(limit=1000)

	def quarantine_opt_in(self, subreddit_name):
		log.debug(f"Opting in to subreddit: {subreddit_name}")
		if not self.no_post:
			try:
				self.reddit.subreddit(subreddit_name).quaran.opt_in()
				self.record_rate_limits()
			except prawcore.exceptions.Forbidden:
				log.info(f"Forbidden opting in to subreddit: {subreddit_name}")
				return False
			except Exception:
				log.warning(f"Error opting in to subreddit: {subreddit_name}")
				log.warning(traceback.format_exc())
				return False
		return True

	def get_user_creation_date(self, user_name):
		log.debug(f"Getting user creation date: {user_name}")
		try:
			created_utc = self.reddit.redditor(user_name).created_utc
			self.record_rate_limits()
			return created_utc
		except Exception:
			return None

	def call_info(self, fullnames):
		log.debug(f"Fetching {len(fullnames)} ids from info")
		result = self.reddit.info(fullnames)
		self.record_rate_limits()
		return result

	def get_subreddit_wiki_page(self, subreddit_name, page_name):
		log.debug(f"Getting subreddit wiki page: {subreddit_name} : {page_name}")
		page_text = None
		try:
			page_text = self.reddit.subreddit(subreddit_name).wiki[page_name].content_md
			log.debug(f"Fetch succeeded: {len(page_text)}")
		except prawcore.exceptions.NotFound:
			log.debug(f"Page doesnt exist")
			pass
		self.record_rate_limits()
		return page_text

	def update_subreddit_wiki_page(self, subreddit_name, page_name, content):
		log.debug(f"Updating subreddit wiki page: {subreddit_name} : {page_name} : {len(content)}")
		self.reddit.subreddit(subreddit_name).wiki[page_name].edit(content=content)
		self.record_rate_limits()
