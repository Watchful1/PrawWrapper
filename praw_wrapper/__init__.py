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


class Queue:
	def __init__(self, max_size):
		self.list = []
		self.max_size = max_size
		self.set = set()

	def put(self, item):
		if len(self.list) >= self.max_size:
			removed_item = self.list.pop(0)
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


class Reddit:
	def __init__(self, user_name, no_post, prefix=None, user_agent=None):
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

		self.processed_comments = Queue(100)
		self.consecutive_timeouts = 0
		self.timeout_warn_threshold = 1
		self.pushshift_lag = 0
		self.pushshift_lag_checked = None

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

	def get_keyword_comments(self, keyword, last_seen):
		if not len(self.processed_comments.list):
			last_seen = last_seen + timedelta(seconds=1)

		log.debug(f"Fetching comments for keyword: {keyword} : {last_seen.strftime('%Y-%m-%d %H:%M:%S')}")
		url = f"https://api.pushshift.io/reddit/comment/search?q={keyword}&limit=100&sort=desc"
		lag_url = "https://api.pushshift.io/reddit/comment/search?limit=1&sort=desc"
		try:
			response = requests.get(url, headers={'User-Agent': self.user_agent}, timeout=10)
			if response.status_code != 200:
				self.consecutive_timeouts += 1
				if self.consecutive_timeouts >= pow(self.timeout_warn_threshold, 2) * 5:
					log.warning(f"{self.consecutive_timeouts} consecutive timeouts for search term: {keyword}")
					self.timeout_warn_threshold += 1
				return []
			comments = response.json()['data']

			if self.pushshift_lag_checked is None or \
					datetime.utcnow() - timedelta(minutes=10) > self.pushshift_lag_checked:
				log.debug("Updating pushshift comment lag")
				json = requests.get(lag_url, headers={'User-Agent': self.user_agent}, timeout=10)
				if json.status_code == 200:
					comment_created = datetime.utcfromtimestamp(json.json()['data'][0]['created_utc'])
					self.pushshift_lag = round((datetime.utcnow() - comment_created).seconds / 60, 0)
					self.pushshift_lag_checked = datetime.utcnow()

			if self.timeout_warn_threshold > 1:
				log.warning(f"Recovered from timeouts after {self.consecutive_timeouts} attempts")

			self.consecutive_timeouts = 0
			self.timeout_warn_threshold = 1

		except requests.exceptions.ReadTimeout:
			self.consecutive_timeouts += 1
			if self.consecutive_timeouts >= pow(self.timeout_warn_threshold, 2) * 5:
				log.warning(f"{self.consecutive_timeouts} consecutive timeouts for search term: {keyword}")
				self.timeout_warn_threshold += 1
			return []

		except Exception as err:
			log.warning(f"Could not parse data for search term: {keyword}")
			log.warning(traceback.format_exc())
			return []

		if not len(comments):
			log.warning(f"No comments found for search term: {keyword}")
			return []

		result_comments = []
		for comment in comments:
			date_time = pytz.utc.localize(datetime.utcfromtimestamp(comment['created_utc']))
			if last_seen > date_time:
				break

			if not self.processed_comments.contains(comment['id']):
				result_comments.append(comment)

		log.debug(f"Found comments: {len(result_comments)}")
		return result_comments

	def mark_keyword_comment_processed(self, comment_id):
		self.processed_comments.put(comment_id)
