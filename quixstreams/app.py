import contextlib
import functools
import logging
import os
import signal
import warnings
from typing import Optional, List, Callable

from confluent_kafka import TopicPartition
from typing_extensions import Self

from .context import set_message_context, copy_context
from .core.stream import Filtered
from .dataframe import StreamingDataFrame
from .error_callbacks import (
    ConsumerErrorCallback,
    ProcessingErrorCallback,
    ProducerErrorCallback,
    default_on_processing_error,
)
from .kafka import (
    AutoOffsetReset,
    Partitioner,
    Producer,
    Consumer,
)
from .logging import configure_logging, LogLevel
from .models import (
    Topic,
    TopicConfig,
    TopicAdmin,
    TopicManager,
    SerializerType,
    DeserializerType,
    TimestampExtractor,
)
from .platforms.quix import (
    QuixKafkaConfigsBuilder,
    check_state_dir,
    check_state_management_enabled,
    QuixTopicManager,
)
from .rowconsumer import RowConsumer
from .rowproducer import RowProducer
from .state import StateStoreManager
from .state.recovery import RecoveryManager
from .state.rocksdb import RocksDBOptionsType

__all__ = ("Application",)

logger = logging.getLogger(__name__)
MessageProcessedCallback = Callable[[str, int, int], None]


class Application:
    """
    The main Application class.

    Typically, the primary object needed to get a kafka application up and running.

    Most functionality is explained the various methods, except for
    "column assignment".


    What it Does:

    - On init:
        - Provides defaults or helper methods for commonly needed objects
        - If `quix_sdk_token` is passed, configures the app to use the Quix Cloud.
    - When executed via `.run()` (after setup):
        - Initializes Topics and StreamingDataFrames
        - Facilitates processing of Kafka messages with a `StreamingDataFrame`
        - Handles all Kafka client consumer/producer responsibilities.


    Example Snippet:

    ```python
    from quixstreams import Application

    # Set up an `app = Application` and  `sdf = StreamingDataFrame`;
    # add some operations to `sdf` and then run everything.

    app = Application(broker_address='localhost:9092', consumer_group='group')
    topic = app.topic('test-topic')
    df = app.dataframe(topic)
    df.apply(lambda value, context: print('New message', value))

    app.run(dataframe=df)
    ```
    """

    def __init__(
        self,
        broker_address: Optional[str] = None,
        quix_sdk_token: Optional[str] = None,
        consumer_group: Optional[str] = None,
        auto_offset_reset: AutoOffsetReset = "latest",
        auto_commit_enable: bool = True,
        partitioner: Partitioner = "murmur2",
        consumer_extra_config: Optional[dict] = None,
        producer_extra_config: Optional[dict] = None,
        state_dir: str = "state",
        rocksdb_options: Optional[RocksDBOptionsType] = None,
        on_consumer_error: Optional[ConsumerErrorCallback] = None,
        on_processing_error: Optional[ProcessingErrorCallback] = None,
        on_producer_error: Optional[ProducerErrorCallback] = None,
        on_message_processed: Optional[MessageProcessedCallback] = None,
        consumer_poll_timeout: float = 1.0,
        producer_poll_timeout: float = 0.0,
        loglevel: Optional[LogLevel] = "INFO",
        auto_create_topics: bool = True,
        use_changelog_topics: bool = True,
        quix_config_builder: Optional[QuixKafkaConfigsBuilder] = None,
        topic_manager: Optional[TopicManager] = None,
    ):
        """
        :param broker_address: Kafka broker host and port in format `<host>:<port>`.
            Passed as `bootstrap.servers` to `confluent_kafka.Consumer`.
            Either this OR `quix_sdk_token` must be set to use `Application` (not both).
            Linked Environment Variable: `Quix__Broker__Address`.
            Default: `None`
        :param quix_sdk_token: If using the Quix Cloud, the SDK token to connect with.
            Either this OR `broker_address` must be set to use Application (not both).
            Linked Environment Variable: `Quix__Sdk__Token`.
            Default: None (if not run on Quix Cloud)
              >***NOTE:*** the environment variable is set for you in the Quix Cloud
        :param consumer_group: Kafka consumer group.
            Passed as `group.id` to `confluent_kafka.Consumer`.
            Linked Environment Variable: `Quix__Consumer__Group`.
            Default - "quixstreams-default" (set during init)
              >***NOTE:*** Quix Applications will prefix it with the Quix workspace id.
        :param auto_offset_reset: Consumer `auto.offset.reset` setting
        :param auto_commit_enable: If true, periodically commit offset of
            the last message handed to the application. Default - `True`.
        :param partitioner: A function to be used to determine the outgoing message
            partition.
        :param consumer_extra_config: A dictionary with additional options that
            will be passed to `confluent_kafka.Consumer` as is.
        :param producer_extra_config: A dictionary with additional options that
            will be passed to `confluent_kafka.Producer` as is.
        :param state_dir: path to the application state directory.
            Default - `".state"`.
        :param rocksdb_options: RocksDB options.
            If `None`, the default options will be used.
        :param consumer_poll_timeout: timeout for `RowConsumer.poll()`. Default - `1.0`s
        :param producer_poll_timeout: timeout for `RowProducer.poll()`. Default - `0`s.
        :param on_message_processed: a callback triggered when message is successfully
            processed.
        :param loglevel: a log level for "quixstreams" logger.
            Should be a string or None.
            If `None` is passed, no logging will be configured.
            You may pass `None` and configure "quixstreams" logger
            externally using `logging` library.
            Default - `"INFO"`.
        :param auto_create_topics: Create all `Topic`s made via Application.topic()
            Default - `True`
        :param use_changelog_topics: Use changelog topics to back stateful operations
            Default - `True`
        :param topic_manager: A `TopicManager` instance

        <br><br>***Error Handlers***<br>
        To handle errors, `Application` accepts callbacks triggered when
            exceptions occur on different stages of stream processing. If the callback
            returns `True`, the exception will be ignored. Otherwise, the exception
            will be propagated and the processing will eventually stop.
        :param on_consumer_error: triggered when internal `RowConsumer` fails
            to poll Kafka or cannot deserialize a message.
        :param on_processing_error: triggered when exception is raised within
            `StreamingDataFrame.process()`.
        :param on_producer_error: triggered when `RowProducer` fails to serialize
            or to produce a message to Kafka.
        <br><br>***Quix Cloud Parameters***<br>
        :param quix_config_builder: instance of `QuixKafkaConfigsBuilder` to be used
            instead of the default one.
            > NOTE: It is recommended to just use `quix_sdk_token` instead.
        """
        configure_logging(loglevel=loglevel)

        # We can't use os.getenv as defaults (and have testing work nicely)
        # since it evaluates getenv when the function is defined.
        # In general this is just a most robust approach.
        broker_address = broker_address or os.getenv("Quix__Broker__Address")
        quix_sdk_token = quix_sdk_token or os.getenv("Quix__Sdk__Token")
        consumer_group = consumer_group or os.getenv(
            "Quix__Consumer_Group", "quixstreams-default"
        )

        if quix_config_builder:
            quix_app_source = "Quix Config Builder"
        if quix_config_builder and quix_sdk_token:
            raise warnings.warn(
                "'quix_config_builder' is not necessary when an SDK token is defined; "
                "we recommend letting the Application generate it automatically"
            )

        if quix_sdk_token and not quix_config_builder:
            quix_app_source = "Quix SDK Token"
            quix_config_builder = QuixKafkaConfigsBuilder(quix_sdk_token=quix_sdk_token)

        if broker_address and quix_config_builder:
            raise ValueError("Cannot provide both broker address and Quix SDK Token")
        elif not (broker_address or quix_config_builder):
            raise ValueError("Either broker address or Quix SDK Token must be provided")
        elif quix_config_builder:
            # SDK Token or QuixKafkaConfigsBuilder were provided
            logger.info(
                f"{quix_app_source} detected; "
                f"the application will connect to Quix Cloud brokers"
            )
            topic_manager_factory = functools.partial(
                QuixTopicManager, quix_config_builder=quix_config_builder
            )
            quix_configs = quix_config_builder.get_confluent_broker_config()
            # Check if the state dir points to the mounted PVC while running on Quix
            # TODO: Do we still need this?
            check_state_dir(state_dir=state_dir)

            broker_address = quix_configs.pop("bootstrap.servers")
            # Quix Cloud prefixes consumer group with workspace id
            consumer_group = quix_config_builder.prepend_workspace_id(consumer_group)
            consumer_extra_config = {**quix_configs, **(consumer_extra_config or {})}
            producer_extra_config = {**quix_configs, **(producer_extra_config or {})}
        else:
            # Only broker address is provided
            topic_manager_factory = TopicManager

        self._is_quix_app = bool(quix_config_builder)

        self._broker_address = broker_address
        self._consumer_group = consumer_group
        self._auto_offset_reset = auto_offset_reset
        self._auto_commit_enable = auto_commit_enable
        self._partitioner = partitioner
        self._producer_extra_config = producer_extra_config
        self._consumer_extra_config = consumer_extra_config

        self._consumer = RowConsumer(
            broker_address=broker_address,
            consumer_group=consumer_group,
            auto_offset_reset=auto_offset_reset,
            auto_commit_enable=auto_commit_enable,
            assignment_strategy="cooperative-sticky",
            extra_config=consumer_extra_config,
            on_error=on_consumer_error,
        )
        self._producer = RowProducer(
            broker_address=broker_address,
            partitioner=partitioner,
            extra_config=producer_extra_config,
            on_error=on_producer_error,
        )

        self._consumer_poll_timeout = consumer_poll_timeout
        self._producer_poll_timeout = producer_poll_timeout
        self._running = False
        self._on_processing_error = on_processing_error or default_on_processing_error
        self._on_message_processed = on_message_processed
        self._auto_create_topics = auto_create_topics
        self._do_recovery_check = False

        if not topic_manager:
            topic_manager = topic_manager_factory(
                topic_admin=TopicAdmin(
                    broker_address=broker_address,
                    extra_config=producer_extra_config,
                )
            )
        self._topic_manager = topic_manager

        self._state_manager = StateStoreManager(
            group_id=consumer_group,
            state_dir=state_dir,
            rocksdb_options=rocksdb_options,
            producer=(
                RowProducer(
                    broker_address=broker_address,
                    partitioner=partitioner,
                    extra_config=producer_extra_config,
                    on_error=on_producer_error,
                )
                if use_changelog_topics
                else None
            ),
            recovery_manager=(
                RecoveryManager(
                    consumer=self._consumer,
                    topic_manager=self._topic_manager,
                )
                if use_changelog_topics
                else None
            ),
        )

    @classmethod
    def Quix(
        cls,
        consumer_group: Optional[str] = None,
        auto_offset_reset: AutoOffsetReset = "latest",
        auto_commit_enable: bool = True,
        partitioner: Partitioner = "murmur2",
        consumer_extra_config: Optional[dict] = None,
        producer_extra_config: Optional[dict] = None,
        state_dir: str = "state",
        rocksdb_options: Optional[RocksDBOptionsType] = None,
        on_consumer_error: Optional[ConsumerErrorCallback] = None,
        on_processing_error: Optional[ProcessingErrorCallback] = None,
        on_producer_error: Optional[ProducerErrorCallback] = None,
        on_message_processed: Optional[MessageProcessedCallback] = None,
        consumer_poll_timeout: float = 1.0,
        producer_poll_timeout: float = 0.0,
        loglevel: Optional[LogLevel] = "INFO",
        quix_config_builder: Optional[QuixKafkaConfigsBuilder] = None,
        auto_create_topics: bool = True,
        use_changelog_topics: bool = True,
        topic_manager: Optional[QuixTopicManager] = None,
    ) -> Self:
        """
        >***NOTE:*** DEPRECATED: use Application with `quix_sdk_token` argument instead.

        Initialize an Application to work with Quix Cloud,
        assuming environment is properly configured (by default in Quix Cloud).

        It takes the credentials from the environment and configures consumer and
        producer to properly connect to the Quix Cloud.

        >***NOTE:*** Quix Cloud requires `consumer_group` and topic names to be
            prefixed with workspace id.
            If the application is created via `Application.Quix()`, the real consumer
            group will be `<workspace_id>-<consumer_group>`,
            and the real topic names will be `<workspace_id>-<topic_name>`.



        Example Snippet:

        ```python
        from quixstreams import Application

        # Set up an `app = Application.Quix` and `sdf = StreamingDataFrame`;
        # add some operations to `sdf` and then run everything. Also shows off how to
        # use the quix-specific serializers and deserializers.

        app = Application.Quix()
        input_topic = app.topic("topic-in", value_deserializer="quix")
        output_topic = app.topic("topic-out", value_serializer="quix_timeseries")
        df = app.dataframe(topic_in)
        df = df.to_topic(output_topic)

        app.run(dataframe=df)
        ```

        :param consumer_group: Kafka consumer group.
            Passed as `group.id` to `confluent_kafka.Consumer`.
            Linked Environment Variable: `Quix__Consumer__Group`.
            Default - "quixstreams-default" (set during init).
              >***NOTE:*** Quix Applications will prefix it with the Quix workspace id.
        :param auto_offset_reset: Consumer `auto.offset.reset` setting
        :param auto_commit_enable: If true, periodically commit offset of
            the last message handed to the application. Default - `True`.
        :param partitioner: A function to be used to determine the outgoing message
            partition.
        :param consumer_extra_config: A dictionary with additional options that
            will be passed to `confluent_kafka.Consumer` as is.
        :param producer_extra_config: A dictionary with additional options that
            will be passed to `confluent_kafka.Producer` as is.
        :param state_dir: path to the application state directory.
            Default - `".state"`.
        :param rocksdb_options: RocksDB options.
            If `None`, the default options will be used.
        :param consumer_poll_timeout: timeout for `RowConsumer.poll()`. Default - `1.0`s
        :param producer_poll_timeout: timeout for `RowProducer.poll()`. Default - `0`s.
        :param on_message_processed: a callback triggered when message is successfully
            processed.
        :param loglevel: a log level for "quixstreams" logger.
            Should be a string or `None`.
            If `None` is passed, no logging will be configured.
            You may pass `None` and configure "quixstreams" logger
            externally using `logging` library.
            Default - `"INFO"`.
        :param auto_create_topics: Create all `Topic`s made via `Application.topic()`
            Default - `True`
        :param use_changelog_topics: Use changelog topics to back stateful operations
            Default - `True`
        :param topic_manager: A `QuixTopicManager` instance

        <br><br>***Error Handlers***<br>
        To handle errors, `Application` accepts callbacks triggered when
            exceptions occur on different stages of stream processing. If the callback
            returns `True`, the exception will be ignored. Otherwise, the exception
            will be propagated and the processing will eventually stop.
        :param on_consumer_error: triggered when internal `RowConsumer` fails to poll
            Kafka or cannot deserialize a message.
        :param on_processing_error: triggered when exception is raised within
            `StreamingDataFrame.process()`.
        :param on_producer_error: triggered when RowProducer fails to serialize
            or to produce a message to Kafka.
        <br><br>***Quix Cloud Parameters***<br>
        :param quix_config_builder: instance of `QuixKafkaConfigsBuilder` to be used
            instead of the default one.

        :return: `Application` object
        """
        warnings.warn(
            "Application.Quix() is being deprecated; "
            "To connect to Quix Cloud, "
            'use Application() with "quix_sdk_token" parameter or set the '
            '"Quix__Sdk__Token" environment variable (like with Application.Quix).',
            DeprecationWarning,
        )
        app = cls(
            broker_address=None,
            quix_sdk_token=os.getenv("Quix__Sdk__Token"),
            consumer_group=consumer_group,
            consumer_extra_config=consumer_extra_config,
            producer_extra_config=producer_extra_config,
            auto_offset_reset=auto_offset_reset,
            auto_commit_enable=auto_commit_enable,
            partitioner=partitioner,
            on_consumer_error=on_consumer_error,
            on_processing_error=on_processing_error,
            on_producer_error=on_producer_error,
            on_message_processed=on_message_processed,
            consumer_poll_timeout=consumer_poll_timeout,
            producer_poll_timeout=producer_poll_timeout,
            loglevel=loglevel,
            state_dir=state_dir,
            rocksdb_options=rocksdb_options,
            auto_create_topics=auto_create_topics,
            use_changelog_topics=use_changelog_topics,
            topic_manager=topic_manager,
            quix_config_builder=quix_config_builder,
        )
        return app

    def topic(
        self,
        name: str,
        value_deserializer: DeserializerType = "json",
        key_deserializer: DeserializerType = "bytes",
        value_serializer: SerializerType = "json",
        key_serializer: SerializerType = "bytes",
        config: Optional[TopicConfig] = None,
        timestamp_extractor: Optional[TimestampExtractor] = None,
    ) -> Topic:
        """
        Create a topic definition.

        Allows you to specify serialization that should be used when consuming/producing
        to the topic in the form of a string name (i.e. "json" for JSON) or a
        serialization class instance directly, like JSONSerializer().


        Example Snippet:

        ```python
        from quixstreams import Application

        # Specify an input and output topic for a `StreamingDataFrame` instance,
        # where the output topic requires adjusting the key serializer.

        app = Application()
        input_topic = app.topic("input-topic", value_deserializer="json")
        output_topic = app.topic(
            "output-topic", key_serializer="str", value_serializer=JSONSerializer()
        )
        sdf = app.dataframe(input_topic)
        sdf.to_topic(output_topic)
        ```

        :param name: topic name
            >***NOTE:*** If the application is created via `Quix.Application()`,
              the topic name will be prefixed by Quix workspace id, and it will
              be `<workspace_id>-<name>`
        :param value_deserializer: a deserializer type for values; default="json"
        :param key_deserializer: a deserializer type for keys; default="bytes"
        :param value_serializer: a serializer type for values; default="json"
        :param key_serializer: a serializer type for keys; default="bytes"
        :param config: optional topic configurations (for creation/validation)
            >***NOTE:*** will not create without Application's auto_create_topics set
            to True (is True by default)

        :param timestamp_extractor: a callable that returns a timestamp in
            milliseconds from a deserialized message. Default - `None`.

        Example Snippet:

        ```python
        app = Application(...)


        def custom_ts_extractor(
            value: Any,
            headers: Optional[List[Tuple[str, bytes]]],
            timestamp: float,
            timestamp_type: TimestampType,
        ) -> int:
            return value["timestamp"]

        topic = app.topic("input-topic", timestamp_extractor=custom_ts_extractor)
        ```


        :return: `Topic` object
        """
        return self._topic_manager.topic(
            name=name,
            key_serializer=key_serializer,
            value_serializer=value_serializer,
            key_deserializer=key_deserializer,
            value_deserializer=value_deserializer,
            config=config,
            timestamp_extractor=timestamp_extractor,
        )

    def dataframe(
        self,
        topic: Topic,
    ) -> StreamingDataFrame:
        """
        A simple helper method that generates a `StreamingDataFrame`, which is used
        to define your message processing pipeline.

        See :class:`quixstreams.dataframe.StreamingDataFrame` for more details.


        Example Snippet:

        ```python
        from quixstreams import Application

        # Set up an `app = Application` and  `sdf = StreamingDataFrame`;
        # add some operations to `sdf` and then run everything.

        app = Application(broker_address='localhost:9092', consumer_group='group')
        topic = app.topic('test-topic')
        df = app.dataframe(topic)
        df.apply(lambda value, context: print('New message', value)

        app.run(dataframe=df)
        ```


        :param topic: a `quixstreams.models.Topic` instance
            to be used as an input topic.
        :return: `StreamingDataFrame` object
        """
        sdf = StreamingDataFrame(topic=topic, state_manager=self._state_manager)
        sdf.producer = self._producer
        return sdf

    def stop(self):
        """
        Stop the internal poll loop and the message processing.

        Only necessary when manually managing the lifecycle of the `Application` (
        likely through some sort of threading).

        To otherwise stop an application, either send a `SIGTERM` to the process
        (like Kubernetes does) or perform a typical `KeyboardInterrupt` (`Ctrl+C`).
        """
        self._running = False
        if self._state_manager.using_changelogs:
            self._state_manager.stop_recovery()

    def get_producer(self) -> Producer:
        """
        Create and return a pre-configured Producer instance.
        The Producer is initialized with params passed to Application.

        It's useful for producing data to Kafka outside the standard Application processing flow,
        (e.g. to produce test data into a topic).
        Using this within the StreamingDataFrame functions is not recommended, as it creates a new Producer
        instance each time, which is not optimized for repeated use in a streaming pipeline.

        Example Snippet:

        ```python
        from quixstreams import Application

        app = Application.Quix(...)
        topic = app.topic("input")

        with app.get_producer() as producer:
            for i in range(100):
                producer.produce(topic=topic.name, key=b"key", value=b"value")
        ```
        """
        self._setup_topics()

        return Producer(
            broker_address=self._broker_address,
            partitioner=self._partitioner,
            extra_config=self._producer_extra_config,
        )

    def get_consumer(self) -> Consumer:
        """
        Create and return a pre-configured Consumer instance.
        The Consumer is initialized with params passed to Application.

        It's useful for consuming data from Kafka outside the standard Application processing flow.
        (e.g. to consume test data from a topic).
        Using it within the StreamingDataFrame functions is not recommended, as it creates a new Consumer instance
        each time, which is not optimized for repeated use in a streaming pipeline.

        Note: By default this consumer does not autocommit consumed offsets to allow exactly-once processing.
        To store the offset call store_offsets() after processing a message.
        If autocommit is necessary set `enable.auto.offset.store` to True in the consumer config when creating the app.

        Example Snippet:

        ```python
        from quixstreams import Application

        app = Application.Quix(...)
        topic = app.topic("input")

        with app.get_consumer() as consumer:
            consumer.subscribe([topic.name])
            while True:
                msg = consumer.poll(timeout=1.0)
                if msg is not None:
                    # Process message
                    # Optionally commit the offset
                    # consumer.store_offsets(msg)

        ```
        """
        self._setup_topics()

        return Consumer(
            broker_address=self._broker_address,
            consumer_group=self._consumer_group,
            auto_offset_reset=self._auto_offset_reset,
            auto_commit_enable=self._auto_commit_enable,
            assignment_strategy="cooperative-sticky",
            extra_config=self._consumer_extra_config,
        )

    def clear_state(self):
        """
        Clear the state of the application.
        """
        self._state_manager.clear_stores()

    def _quix_runtime_init(self):
        """
        Do a runtime setup only applicable to an Application.Quix instance
        - Ensure that "State management" flag is enabled for deployment if the app
          is stateful and is running in Quix Cloud
        """
        # Ensure that state management is enabled if application is stateful
        if self._state_manager.stores:
            check_state_management_enabled()

    def _setup_topics(self):
        topics_list = ", ".join(
            f'"{topic.name}"' for topic in self._topic_manager.all_topics
        )
        logger.info(f"Topics required for this application: {topics_list}")
        if self._auto_create_topics:
            self._topic_manager.create_all_topics()
        self._topic_manager.validate_all_topics()

    def _process_message(self, dataframe_composed, start_state_transaction):
        # Serve producer callbacks
        self._producer.poll(self._producer_poll_timeout)
        rows = self._consumer.poll_row(timeout=self._consumer_poll_timeout)

        if rows is None:
            return

        # Deserializer may return multiple rows for a single message
        rows = rows if isinstance(rows, list) else [rows]
        if not rows:
            return

        first_row = rows[0]
        topic_name, partition, offset = (
            first_row.topic,
            first_row.partition,
            first_row.offset,
        )

        with start_state_transaction(
            topic=topic_name, partition=partition, offset=offset
        ):
            for row in rows:
                context = copy_context()
                context.run(set_message_context, first_row.context)
                try:
                    # Execute StreamingDataFrame in a context
                    context.run(dataframe_composed, row.value)
                except Filtered:
                    # The message was filtered by StreamingDataFrame
                    continue
                except Exception as exc:
                    # TODO: This callback might be triggered because of Producer
                    #  errors too because they happen within ".process()"
                    to_suppress = self._on_processing_error(exc, row, logger)
                    if not to_suppress:
                        raise

        # Store the message offset after it's successfully processed
        self._consumer.store_offsets(
            offsets=[
                TopicPartition(
                    topic=topic_name,
                    partition=partition,
                    offset=offset + 1,
                )
            ]
        )

        if self._on_message_processed is not None:
            self._on_message_processed(topic_name, partition, offset)

    def run(
        self,
        dataframe: StreamingDataFrame,
    ):
        """
        Start processing data from Kafka using provided `StreamingDataFrame`

        One started, can be safely terminated with a `SIGTERM` signal
        (like Kubernetes does) or a typical `KeyboardInterrupt` (`Ctrl+C`).


        Example Snippet:

        ```python
        from quixstreams import Application

        # Set up an `app = Application` and  `sdf = StreamingDataFrame`;
        # add some operations to `sdf` and then run everything.

        app = Application(broker_address='localhost:9092', consumer_group='group')
        topic = app.topic('test-topic')
        df = app.dataframe(topic)
        df.apply(lambda value, context: print('New message', value)

        app.run(dataframe=df)
        ```

        :param dataframe: instance of `StreamingDataFrame`
        """
        self._setup_signal_handlers()

        logger.info(
            f"Starting the Application with the config: "
            f'broker_address="{self._broker_address}" '
            f'consumer_group="{self._consumer_group}" '
            f'auto_offset_reset="{self._auto_offset_reset}"'
        )
        if self._is_quix_app:
            self._quix_runtime_init()

        self._setup_topics()

        exit_stack = contextlib.ExitStack()
        exit_stack.enter_context(self._producer)
        exit_stack.enter_context(self._consumer)
        exit_stack.enter_context(self._state_manager)

        exit_stack.callback(
            lambda *_: logger.debug("Closing Kafka consumers & producers")
        )
        exit_stack.callback(lambda *_: self.stop())

        if self._state_manager.stores:
            # Store manager has stores registered, use real state transactions
            # during processing
            start_state_transaction = self._state_manager.start_store_transaction
        else:
            # Application is stateless, use dummy state transactions
            start_state_transaction = _dummy_state_transaction

        with exit_stack:
            # Subscribe to topics in Kafka and start polling
            self._consumer.subscribe(
                [dataframe.topic],
                on_assign=self._on_assign,
                on_revoke=self._on_revoke,
                on_lost=self._on_lost,
            )
            logger.info("Waiting for incoming messages")
            # Start polling Kafka for messages and callbacks
            self._running = True

            dataframe_composed = dataframe.compose()

            while self._running:
                if self._state_manager.recovery_required:
                    self._state_manager.do_recovery()
                else:
                    self._process_message(dataframe_composed, start_state_transaction)

            logger.info("Stop processing of StreamingDataFrame")

    def _on_assign(self, _, topic_partitions: List[TopicPartition]):
        """
        Assign new topic partitions to consumer and state.

        :param topic_partitions: list of `TopicPartition` from Kafka
        """
        # sometimes "empty" calls happen, probably updating the consumer epoch
        if not topic_partitions:
            return
        # assigning manually here (instead of allowing it handle it automatically)
        # enables pausing them during recovery to work as expected
        self._consumer.incremental_assign(topic_partitions)

        if self._state_manager.stores:
            logger.debug(f"Rebalancing: assigning state store partitions")
            for tp in topic_partitions:
                # Assign store partitions
                store_partitions = self._state_manager.on_partition_assign(tp)

                # Check if the latest committed offset >= stored offset
                # Otherwise, the re-processed messages might use already updated
                # state, which can lead to inconsistent outputs
                stored_offsets = [
                    offset
                    for offset in (
                        store_tp.get_processed_offset() for store_tp in store_partitions
                    )
                    if offset is not None
                ]
                min_stored_offset = min(stored_offsets) + 1 if stored_offsets else None
                if min_stored_offset is not None:
                    tp_committed = self._consumer.committed([tp], timeout=30)[0]
                    if min_stored_offset > tp_committed.offset:
                        logger.warning(
                            f'Warning: offset "{tp_committed.offset}" '
                            f"for topic partition "
                            f'"{tp_committed.topic}[{tp_committed.partition}]" '
                            f'is behind the stored offset "{min_stored_offset}". '
                            f"It may lead to distortions in produced data."
                        )

    def _on_revoke(self, _, topic_partitions: List[TopicPartition]):
        """
        Revoke partitions from consumer and state
        """
        self._consumer.incremental_unassign(topic_partitions)
        if self._state_manager.stores:
            logger.debug(f"Rebalancing: revoking state store partitions")
            for tp in topic_partitions:
                self._state_manager.on_partition_revoke(tp)

    def _on_lost(self, _, topic_partitions: List[TopicPartition]):
        """
        Dropping lost partitions from consumer and state
        """
        if self._state_manager.stores:
            logger.debug(f"Rebalancing: dropping lost state store partitions")
            for tp in topic_partitions:
                self._state_manager.on_partition_lost(tp)

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._on_sigint)
        signal.signal(signal.SIGTERM, self._on_sigterm)

    def _on_sigint(self, *_):
        # Re-install the default SIGINT handler so doing Ctrl+C twice
        # raises KeyboardInterrupt
        signal.signal(signal.SIGINT, signal.default_int_handler)
        logger.debug(f"Received SIGINT, stopping the processing loop")
        self.stop()

    def _on_sigterm(self, *_):
        logger.debug(f"Received SIGTERM, stopping the processing loop")
        self.stop()


_nullcontext = contextlib.nullcontext()


def _dummy_state_transaction(topic: str, partition: int, offset: int):
    return _nullcontext
