#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2020 Confluent Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import io
import sys
import base64
import struct
import warnings
from collections import deque

from google.protobuf.message import DecodeError
from google.protobuf.message_factory import MessageFactory

from . import (_MAGIC_BYTE,
               reference_subject_name_strategy,
               topic_subject_name_strategy,)
from .schema_registry_client import (Schema,
                                     SchemaReference)
from confluent_kafka.serialization import SerializationError

# Converts an int to bytes (opposite of ord)
# Python3.chr() -> Unicode
# Python2.chr() -> str(alias for bytes)
if sys.version > '3':
    def _bytes(b):
        """
        Convert int to bytes

        Args:
            b (int): int to format as bytes.

        """
        return bytes((b,))
else:
    def _bytes(b):
        """
        Convert int to bytes

        Args:
            b (int): int to format as bytes.

        """
        return chr(b)


class _ContextStringIO(io.BytesIO):
    """
    Wrapper to allow use of StringIO via 'with' constructs.

    """

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _create_msg_index(msg_desc):
    """
    Maps the location of msg_desc within a FileDescriptor.

    Args:
        msg_desc (MessageDescriptor): Protobuf MessageDescriptor

    Returns:
        [int]: Protobuf MessageDescriptor index

    Raises:
        ValueError: If the message descriptor is malformed.

    """
    msg_idx = deque()
    current = msg_desc
    found = False
    # Traverse tree upwardly it's root
    while current.containing_type is not None:
        previous = current
        current = previous.containing_type
        # find child's position
        for idx, node in enumerate(current.nested_types):
            if node == previous:
                msg_idx.appendleft(idx)
                found = True
                break
        if not found:
            raise ValueError("Nested MessageDescriptor not found")

    found = False
    # find root's position in protofile
    for idx, msg_type_name in enumerate(msg_desc.file.message_types_by_name):
        if msg_type_name == current.name:
            msg_idx.appendleft(idx)
            found = True
            break
    if not found:
        raise ValueError("MessageDescriptor not found in file")

    return list(msg_idx)


def _schema_to_str(proto_file):
    """
    Base64 encodes a FileDescriptor

    Args:
        proto_file (FileDescriptor): FileDescriptor to encode.

    Returns:
        str: Base64 encoded FileDescriptor

    """
    return base64.standard_b64encode(proto_file.serialized_pb).decode('ascii')


class ProtobufSerializer(object):
    """
    ProtobufSerializer serializes objects in the Confluent Schema Registry
    binary format for Protobuf.

    ProtobufSerializer configuration properties:

    +-------------------------------------+----------+------------------------------------------------------+
    | Property Name                       | Type     | Description                                          |
    +=====================================+==========+======================================================+
    |                                     |          | Registers schemas automatically if not               |
    | ``auto.register.schemas``           | bool     | previously associated with a particular subject.     |
    |                                     |          | Defaults to True.                                    |
    +-------------------------------------+----------+------------------------------------------------------+
    |                                     |          | Whether to normalize schemas, which will             |
    | ``normalize.schemas``               | bool     | transform schemas to have a consistent format,       |
    |                                     |          | including ordering properties and references.        |
    +---------------------------+----------+----------------------------------------------------------------+
    |                                     |          | Whether to use the latest subject version for        |
    | ``use.latest.version``              | bool     | serialization.                                       |
    |                                     |          | WARNING: There is no check that the latest           |
    |                                     |          | schema is backwards compatible with the object       |
    |                                     |          | being serialized.                                    |
    |                                     |          | Defaults to False.                                   |
    +-------------------------------------+----------+------------------------------------------------------+
    |                                     |          | Whether to skip known types when resolving schema    |
    | ``skip.known.types``                | bool     | dependencies.                                        |
    |                                     |          | Defaults to False.                                   |
    +-------------------------------------+----------+------------------------------------------------------+
    |                                     |          | Callable(SerializationContext, str) -> str           |
    |                                     |          |                                                      |
    | ``subject.name.strategy``           | callable | Instructs the ProtobufSerializer on how to construct |
    |                                     |          | Schema Registry subject names.                       |
    |                                     |          | Defaults to topic_subject_name_strategy.             |
    +-------------------------------------+----------+------------------------------------------------------+
    |                                     |          | Callable(SerializationContext, str) -> str           |
    |                                     |          |                                                      |
    | ``reference.subject.name.strategy`` | callable | Instructs the ProtobufSerializer on how to construct |
    |                                     |          | Schema Registry subject names for Schema References  |
    |                                     |          | Defaults to reference_subject_name_strategy          |
    +-------------------------------------+----------+------------------------------------------------------+
    | ``use.deprecated.format``           | bool     | Specifies whether the Protobuf serializer should     |
    |                                     |          | serialize message indexes without zig-zag encoding.  |
    |                                     |          | This option must be explicitly configured as older   |
    |                                     |          | and newer Protobuf producers are incompatible.       |
    |                                     |          | If the consumers of the topic being produced to are  |
    |                                     |          | using confluent-kafka-python <1.8 then this property |
    |                                     |          | must be set to True until all old consumers have     |
    |                                     |          | have been upgraded.                                 |
    |                                     |          | Warning: This configuration property will be removed |
    |                                     |          | in a future version of the client.                   |
    +-------------------------------------+----------+------------------------------------------------------+

    Schemas are registered to namespaces known as Subjects which define how a
    schema may evolve over time. By default the subject name is formed by
    concatenating the topic name with the message field separated by a hyphen.

    i.e. {topic name}-{message field}

    Alternative naming strategies may be configured with the property
    ``subject.name.strategy``.

    Supported subject name strategies

    +--------------------------------------+------------------------------+
    | Subject Name Strategy                | Output Format                |
    +======================================+==============================+
    | topic_subject_name_strategy(default) | {topic name}-{message field} |
    +--------------------------------------+------------------------------+
    | topic_record_subject_name_strategy   | {topic name}-{record name}   |
    +--------------------------------------+------------------------------+
    | record_subject_name_strategy         | {record name}                |
    +--------------------------------------+------------------------------+

    See `Subject name strategy <https://docs.confluent.io/current/schema-registry/serializer-formatter.html#subject-name-strategy>`_ for additional details.

    Args:
        msg_type (GeneratedProtocolMessageType): Protobuf Message type.

        schema_registry_client (SchemaRegistryClient): Schema Registry
            client instance.

        conf (dict): ProtobufSerializer configuration.

    See Also:
        `Protobuf API reference <https://googleapis.dev/python/protobuf/latest/google/protobuf.html>`_

    """  # noqa: E501
    __slots__ = ['_auto_register', '_normalize_schemas', '_use_latest_version', '_skip_known_types',
                 '_registry', '_known_subjects',
                 '_msg_class', '_msg_index', '_schema', '_schema_id',
                 '_ref_reference_subject_func', '_subject_name_func',
                 '_use_deprecated_format']
    # default configuration
    _default_conf = {
        'auto.register.schemas': True,
        'normalize.schemas': False,
        'use.latest.version': False,
        'skip.known.types': False,
        'subject.name.strategy': topic_subject_name_strategy,
        'reference.subject.name.strategy': reference_subject_name_strategy,
        'use.deprecated.format': False,
    }

    def __init__(self, msg_type, schema_registry_client, conf=None):

        if conf is None or 'use.deprecated.format' not in conf:
            raise RuntimeError(
                "ProtobufSerializer: the 'use.deprecated.format' configuration "
                "property must be explicitly set due to backward incompatibility "
                "with older confluent-kafka-python Protobuf producers and consumers. "
                "See the release notes for more details")

        # handle configuration
        conf_copy = self._default_conf.copy()
        if conf is not None:
            conf_copy.update(conf)

        self._auto_register = conf_copy.pop('auto.register.schemas')
        if not isinstance(self._auto_register, bool):
            raise ValueError("auto.register.schemas must be a boolean value")

        self._normalize_schemas = conf_copy.pop('normalize.schemas')
        if not isinstance(self._normalize_schemas, bool):
            raise ValueError("normalize.schemas must be a boolean value")

        self._use_latest_version = conf_copy.pop('use.latest.version')
        if not isinstance(self._use_latest_version, bool):
            raise ValueError("use.latest.version must be a boolean value")
        if self._use_latest_version and self._auto_register:
            raise ValueError("cannot enable both use.latest.version and auto.register.schemas")

        self._skip_known_types = conf_copy.pop('skip.known.types')
        if not isinstance(self._skip_known_types, bool):
            raise ValueError("skip.known.types must be a boolean value")

        self._use_deprecated_format = conf_copy.pop('use.deprecated.format')
        if not isinstance(self._use_deprecated_format, bool):
            raise ValueError("use.deprecated.format must be a boolean value")
        if self._use_deprecated_format:
            warnings.warn("ProtobufSerializer: the 'use.deprecated.format' "
                          "configuration property, and the ability to use the "
                          "old incorrect Protobuf serializer heading format "
                          "introduced in confluent-kafka-python v1.4.0, "
                          "will be removed in an upcoming release in 2021 Q2. "
                          "Please migrate your Python Protobuf producers and "
                          "consumers to 'use.deprecated.format':False as "
                          "soon as possible")

        self._subject_name_func = conf_copy.pop('subject.name.strategy')
        if not callable(self._subject_name_func):
            raise ValueError("subject.name.strategy must be callable")

        self._ref_reference_subject_func = conf_copy.pop(
            'reference.subject.name.strategy')
        if not callable(self._ref_reference_subject_func):
            raise ValueError("subject.name.strategy must be callable")

        if len(conf_copy) > 0:
            raise ValueError("Unrecognized properties: {}"
                             .format(", ".join(conf_copy.keys())))

        self._registry = schema_registry_client
        self._schema_id = None
        # Avoid calling registry if schema is known to be registered
        self._known_subjects = set()
        self._msg_class = msg_type

        descriptor = msg_type.DESCRIPTOR
        self._msg_index = _create_msg_index(descriptor)
        self._schema = Schema(_schema_to_str(descriptor.file),
                              schema_type='PROTOBUF')

    @staticmethod
    def _write_varint(buf, val, zigzag=True):
        """
        Writes val to buf, either using zigzag or uvarint encoding.

        Args:
            buf (BytesIO): buffer to write to.
            val (int): integer to be encoded.
            zigzag (bool): whether to encode in zigzag or uvarint encoding
        """

        if zigzag:
            val = (val << 1) ^ (val >> 63)

        while (val & ~0x7f) != 0:
            buf.write(_bytes((val & 0x7f) | 0x80))
            val >>= 7
        buf.write(_bytes(val))

    @staticmethod
    def _encode_varints(buf, ints, zigzag=True):
        """
        Encodes each int as a uvarint onto buf

        Args:
            buf (BytesIO): buffer to write to.
            ints ([int]): ints to be encoded.
            zigzag (bool): whether to encode in zigzag or uvarint encoding

        """

        assert len(ints) > 0
        # The root element at the 0 position does not need a length prefix.
        if ints == [0]:
            buf.write(_bytes(0x00))
            return

        ProtobufSerializer._write_varint(buf, len(ints), zigzag=zigzag)

        for value in ints:
            ProtobufSerializer._write_varint(buf, value, zigzag=zigzag)

    def _resolve_dependencies(self, ctx, file_desc):
        """
        Resolves and optionally registers schema references recursively.

        Args:
            ctx (SerializationContext): Serialization context.

            file_desc (FileDescriptor): file descriptor to traverse.

        """
        schema_refs = []
        for dep in file_desc.dependencies:
            if self._skip_known_types and dep.name.startswith("google/protobuf/"):
                continue
            dep_refs = self._resolve_dependencies(ctx, dep)
            subject = self._ref_reference_subject_func(ctx, dep)
            schema = Schema(_schema_to_str(dep),
                            references=dep_refs,
                            schema_type='PROTOBUF')
            if self._auto_register:
                self._registry.register_schema(subject, schema)

            reference = self._registry.lookup_schema(subject, schema)
            # schema_refs are per file descriptor
            schema_refs.append(SchemaReference(dep.name,
                                               subject,
                                               reference.version))
        return schema_refs

    def __call__(self, message_type, ctx):
        """
        Serializes a Protobuf Message to the Confluent Schema Registry
        Protobuf binary format.

        Args:
            message_type (Message): Protobuf message instance.

            ctx (SerializationContext): Metadata pertaining to the serialization
                operation.

        Note:
            None objects are represented as Kafka Null.

        Raises:
            SerializerError if any error occurs serializing obj

        Returns:
            bytes: Confluent Schema Registry formatted Protobuf bytes

        """
        if message_type is None:
            return None

        if not isinstance(message_type, self._msg_class):
            raise ValueError("message must be of type {} not {}"
                             .format(self._msg_class, type(message_type)))

        subject = self._subject_name_func(ctx,
                                          message_type.DESCRIPTOR.full_name)

        if subject not in self._known_subjects:
            if self._use_latest_version:
                latest_schema = self._registry.get_latest_version(subject)
                self._schema_id = latest_schema.schema_id

            else:
                self._schema.references = self._resolve_dependencies(
                    ctx, message_type.DESCRIPTOR.file)

                if self._auto_register:
                    self._schema_id = self._registry.register_schema(subject,
                                                                     self._schema,
                                                                     self._normalize_schemas)
                else:
                    self._schema_id = self._registry.lookup_schema(
                        subject, self._schema, self._normalize_schemas).schema_id

            self._known_subjects.add(subject)

        with _ContextStringIO() as fo:
            # Write the magic byte and schema ID in network byte order
            # (big endian)
            fo.write(struct.pack('>bI', _MAGIC_BYTE, self._schema_id))
            # write the record index to the buffer
            self._encode_varints(fo, self._msg_index,
                                 zigzag=not self._use_deprecated_format)
            # write the record itself
            fo.write(message_type.SerializeToString())
            return fo.getvalue()


class ProtobufDeserializer(object):
    """
    ProtobufDeserializer decodes bytes written in the Schema Registry
    Protobuf format to an object.

    Args:
        message_type (GeneratedProtocolMessageType): Protobuf Message type.
        conf (dict): Configuration dictionary.

    ProtobufDeserializer configuration properties:

    +-------------------------------------+----------+------------------------------------------------------+
    | Property Name                       | Type     | Description                                          |
    +-------------------------------------+----------+------------------------------------------------------+
    | ``use.deprecated.format``           | bool     | Specifies whether the Protobuf deserializer should   |
    |                                     |          | deserialize message indexes without zig-zag encoding.|
    |                                     |          | This option must be explicitly configured as older   |
    |                                     |          | and newer Protobuf producers are incompatible.       |
    |                                     |          | If Protobuf messages in the topic to consume were    |
    |                                     |          | produced with confluent-kafka-python <1.8 then this  |
    |                                     |          | property must be set to True until all old messages  |
    |                                     |          | have been processed and producers have been upgraded.|
    |                                     |          | Warning: This configuration property will be removed |
    |                                     |          | in a future version of the client.                   |
    +-------------------------------------+----------+------------------------------------------------------+


    See Also:
    `Protobuf API reference <https://googleapis.dev/python/protobuf/latest/google/protobuf.html>`_

    """
    __slots__ = ['_msg_class', '_msg_index', '_use_deprecated_format']

    # default configuration
    _default_conf = {
        'use.deprecated.format': False,
    }

    def __init__(self, message_type, conf=None):

        # Require use.deprecated.format to be explicitly configured
        # during a transitionary period since old/new format are
        # incompatible.
        if conf is None or 'use.deprecated.format' not in conf:
            raise RuntimeError(
                "ProtobufDeserializer: the 'use.deprecated.format' configuration "
                "property must be explicitly set due to backward incompatibility "
                "with older confluent-kafka-python Protobuf producers and consumers. "
                "See the release notes for more details")

        # handle configuration
        conf_copy = self._default_conf.copy()
        if conf is not None:
            conf_copy.update(conf)

        self._use_deprecated_format = conf_copy.pop('use.deprecated.format')
        if not isinstance(self._use_deprecated_format, bool):
            raise ValueError("use.deprecated.format must be a boolean value")
        if self._use_deprecated_format:
            warnings.warn("ProtobufDeserializer: the 'use.deprecated.format' "
                          "configuration property, and the ability to use the "
                          "old incorrect Protobuf serializer heading format "
                          "introduced in confluent-kafka-python v1.4.0, "
                          "will be removed in an upcoming release in 2022 Q2. "
                          "Please migrate your Python Protobuf producers and "
                          "consumers to 'use.deprecated.format':False as "
                          "soon as possible")

        descriptor = message_type.DESCRIPTOR
        self._msg_index = _create_msg_index(descriptor)
        self._msg_class = MessageFactory().GetPrototype(descriptor)

    @staticmethod
    def _decode_varint(buf, zigzag=True):
        """
        Decodes a single varint from a buffer.

        Args:
            buf (BytesIO): buffer to read from
            zigzag (bool): decode as zigzag or uvarint

        Returns:
            int: decoded varint

        Raises:
            EOFError: if buffer is empty

        """
        value = 0
        shift = 0
        try:
            while True:
                i = ProtobufDeserializer._read_byte(buf)

                value |= (i & 0x7f) << shift
                shift += 7
                if not (i & 0x80):
                    break

            if zigzag:
                value = (value >> 1) ^ -(value & 1)

            return value

        except EOFError:
            raise EOFError("Unexpected EOF while reading index")

    @staticmethod
    def _read_byte(buf):
        """
        Returns int representation for a byte.

        Args:
            buf (BytesIO): buffer to read from

        .. _ord:
            https://docs.python.org/2/library/functions.html#ord
        """
        i = buf.read(1)
        if i == b'':
            raise EOFError("Unexpected EOF encountered")
        return ord(i)

    @staticmethod
    def _decode_index(buf, zigzag=True):
        """
        Extracts message index from Schema Registry Protobuf formatted bytes.

        Args:
            buf (BytesIO): byte buffer

        Returns:
            int: Protobuf Message index.

        """
        size = ProtobufDeserializer._decode_varint(buf, zigzag=zigzag)
        if size < 0 or size > 100000:
            raise DecodeError("Invalid Protobuf msgidx array length")

        if size == 0:
            return [0]

        msg_index = []
        for _ in range(size):
            msg_index.append(ProtobufDeserializer._decode_varint(buf,
                                                                 zigzag=zigzag))

        return msg_index

    def __call__(self, value, ctx):
        """
        Deserializes Schema Registry formatted Protobuf to Protobuf Message.

        Args:
            value (bytes): Confluent Schema Registry formatted Protobuf bytes.

            ctx (SerializationContext): Metadata pertaining to the serialization
                operation.

        Returns:
            Message: Protobuf Message instance.

        Raises:
            SerializerError: If response payload and expected Message type
            differ.

        """
        if value is None:
            return None

        # SR wire protocol + msg_index length
        if len(value) < 6:
            raise SerializationError("Message too small. This message was not"
                                     " produced with a Confluent"
                                     " Schema Registry serializer")

        with _ContextStringIO(value) as payload:
            magic, schema_id = struct.unpack('>bI', payload.read(5))
            if magic != _MAGIC_BYTE:
                raise SerializationError("Unknown magic byte. This message was"
                                         " not produced with a Confluent"
                                         " Schema Registry serializer")

            # Protobuf Messages are self-describing; no need to query schema
            # Move the reader cursor past the index
            _ = self._decode_index(payload, zigzag=not self._use_deprecated_format)
            msg = self._msg_class()
            try:
                msg.ParseFromString(payload.read())
            except DecodeError as e:
                raise SerializationError(str(e))

            return msg
