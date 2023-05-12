from sqlalchemy import create_engine, UniqueConstraint, Column, ForeignKey, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, joinedload
import os
import logging.handlers
from shutil import copyfile
from datetime import datetime
from enum import Enum


log = logging.getLogger("bot")


Base = declarative_base()


class IngestDatabase:
	def __init__(self, location="database.db", default_client_id=None):
		self.engine = None
		self.session = None
		self.location = location
		self.default_client_id = default_client_id
		self.init(self.location)

	def init(self, location):
		self.engine = create_engine(f'sqlite:///{location}')
		session_maker = sessionmaker(bind=self.engine)
		self.session = session_maker()
		Base.metadata.create_all(self.engine)
		self.session.commit()

	def commit(self):
		self.session.commit()

	def close(self):
		self.session.commit()
		self.engine.dispose()

	def backup(self, backup_folder="backup"):
		self.close()

		if not os.path.exists(backup_folder):
			os.makedirs(backup_folder)

		copyfile(
			self.location,
			backup_folder + "/" +
				datetime.utcnow().strftime("%Y-%m-%d_%H-%M") +
				".db"
		)

		self.init(self.location)

	def get_or_add_client(self, client_name):
		log.debug(f"Fetching client: {client_name} : {self.default_client_id}")

		if client_name is not None:
			client = self.session.query(Client).filter_by(name=client_name).first()
		elif self.default_client_id is not None:
			client = self.session.query(Client).filter_by(id=self.default_client_id).first()
		else:
			log.warning("Client name and id not set in get client")
			return

		if client is None:
			log.debug(f"Creating client: {client_name}")
			client = Client(client_name)
			self.session.add(client)

		return client

	def register_search(self, search_term, client_name=None):
		log.debug(f"Registering search: {client_name} : {search_term}")
		client = self.get_or_add_client(client_name)
		search = self.session.query(Search).filter_by(client_id=client.id).filter_by(search_term=search_term).first()
		if search is None:
			log.debug(f"Creating search: {client_name} : {search_term}")
			search = Search(client, search_term)
			self.session.add(search)

	def get_all_searches(self):
		log.debug(f"Fetching searches")
		searches = self.session.query(Search).options(joinedload(Search.client)).all()
		log.debug(f"Found searches: {len(searches)}")
		search_map = {}
		for search in searches:
			if search.client.id in search_map:
				search_map[search.client.id].append(search.search_term)
			else:
				search_map[search.client.id] = [search.search_term]
		return search_map

	def get_comments(self, client=None, limit=100):
		log.debug(f"Fetching comments")
		if client is not None:
			client_id = client.id
		elif self.default_client_id is not None:
			client_id = self.default_client_id
		else:
			log.warning("Client name and id not set in get comments")
			return None
		comments = self.session.query(IngestComment).filter_by(client_id=client_id).order_by(IngestComment.created_utc.asc()).limit(limit).all()
		log.debug(f"Found comments: {len(comments)}")
		return comments

	def add_comment(self, comment):
		log.debug(f"Adding comment: {comment.client_id} : {comment.id}")
		self.session.add(comment)

	def delete_comment(self, comment):
		log.debug(f"Deleting comment: {comment.client_id} : {comment.id}")
		self.session.delete(comment)

	def save_keystore(self, key, value):
		log.debug(f"Saving keystore: {key} : {value}")
		self.session.merge(KeyValue(key, value))

	def get_keystore(self, key):
		log.debug(f"Fetching keystore: {key}")
		key_value = self.session.query(KeyValue).filter_by(key=key).first()

		if key_value is None:
			log.debug("Key not found")
			return None

		log.debug(f"Value: {key_value.value}")
		return key_value.value


class Client(Base):
	__tablename__ = 'clients'
	__table_args__ = (
		UniqueConstraint("name", name="_clients_name"),
	)

	id = Column(Integer, primary_key=True)
	name = Column(String(80), nullable=False)

	def __init__(
		self,
		name
	):
		self.name = name


class Search(Base):
	__tablename__ = 'search'
	__table_args__ = (
		UniqueConstraint('client_id', 'search_term', name='_search_client_term'),
	)

	id = Column(Integer, primary_key=True)
	client_id = Column(Integer, ForeignKey('clients.id'))
	search_term = Column(String(80), nullable=False)

	client = relationship("Client")

	def __init__(
		self,
		client,
		search_term
	):
		self.client = client
		self.search_term = search_term


class IngestComment(Base):
	__tablename__ = 'ingest_comments'

	id = Column(String(12), primary_key=True)
	client_id = Column(Integer, ForeignKey('clients.id'), primary_key=True)
	author = Column(String(80), nullable=False)
	subreddit = Column(String(80), nullable=False)
	created_utc = Column(Integer, nullable=False)
	permalink = Column(String(400), nullable=False)
	link_id = Column(String(12), nullable=False)
	body = Column(String(10000), nullable=False)

	client = relationship("Client")

	def __init__(
		self,
		id,
		author,
		subreddit,
		created_utc,
		permalink,
		link_id,
		body,
		client_id
	):
		self.id = id
		self.author = author
		self.subreddit = subreddit
		self.created_utc = created_utc
		self.permalink = permalink
		self.link_id = link_id
		self.body = body
		self.client_id = client_id


class KeyValue(Base):
	__tablename__ = 'key_value'

	key = Column(String(32), primary_key=True)
	value = Column(String(200))

	def __init__(
		self,
		key,
		value
	):
		self.key = key
		self.value = value
