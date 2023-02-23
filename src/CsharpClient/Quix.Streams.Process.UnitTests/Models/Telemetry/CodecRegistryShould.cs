﻿using System.Linq;
using FluentAssertions;
using Quix.Streams.Process.Models;
using Quix.Streams.Process.Models.Codecs;
using Quix.Streams.Process.Models.Telemetry.Parameters.Codecs;
using Quix.Streams.Transport.Fw;
using Quix.Streams.Transport.Fw.Codecs;
using Xunit;

namespace Quix.Streams.Process.UnitTests.Models.Telemetry
{
    public class CodecRegistryShould
    {
        private void ValidateForDefaultJsonCodec<T>()
        {
            var codecs = Transport.Registry.CodecRegistry.RetrieveCodecs(new ModelKey(typeof(T).Name));
            var writeCodec = codecs.FirstOrDefault();
            writeCodec.Should().NotBeNull();
            writeCodec.GetType().IsAssignableFrom(typeof(DefaultJsonCodec<T>)).Should().BeTrue($"expecting DefaultJsonCodec<{typeof(T).Name}>");
        }
        
        [Fact]
        public void Register_JsonEvents_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.Json);
            
            // Assert
            var codecs = Transport.Registry.CodecRegistry.RetrieveCodecs("EventData[]");
            var writeCodec = codecs.FirstOrDefault();
            writeCodec.Should().NotBeNull();
            writeCodec.GetType().IsAssignableFrom(typeof(DefaultJsonCodec<EventDataRaw[]>)).Should().BeTrue($"expecting DefaultJsonCodec<EventData[]>");
        }
        
        [Fact]
        public void Register_ImprovedJsonTimeseriesData_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.ImprovedJson);
            
            // Assert
            var codecs = Transport.Registry.CodecRegistry.RetrieveCodecs(new ModelKey("TimeseriesData"));
            codecs.Count().Should().Be(3);
            codecs.Should().Contain(x => x is DefaultJsonCodec<TimeseriesDataRaw>); // for reading
            codecs.Should().Contain(x => x is TimeseriesDataJsonCodec);
            codecs.Should().Contain(x => x is TimeseriesDataProtobufCodec); // for reading
            codecs.First().GetType().Should().Be(typeof(TimeseriesDataJsonCodec)); // for writing
        }
        
        [Fact]
        public void Register_JsonTimeseriesData_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.Json);
            
            // Assert
            var codecs = Transport.Registry.CodecRegistry.RetrieveCodecs(new ModelKey("TimeseriesData"));
            codecs.Count().Should().Be(3);
            codecs.Should().Contain(x => x is DefaultJsonCodec<TimeseriesDataRaw>); 
            codecs.Should().Contain(x => x is TimeseriesDataJsonCodec); // for reading
            codecs.Should().Contain(x => x is TimeseriesDataProtobufCodec); // for reading
            codecs.First().GetType().Should().Be(typeof(DefaultJsonCodec<TimeseriesDataRaw>)); // for writing
        }
        
        [Fact]
        public void Register_JsonStreamProperties_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.Json);
            
            // Assert
            ValidateForDefaultJsonCodec<StreamProperties>();
        }
        
        [Fact]
        public void Register_JsonParameterDefinitions_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.Json);
            
            // Assert
            ValidateForDefaultJsonCodec<Process.Models.ParameterDefinitions>();
        }

        [Fact]
        public void Register_JsonEventDefinitions_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.Json);

            // Assert
            ValidateForDefaultJsonCodec<Process.Models.EventDefinitions>();
        }


        [Fact]
        public void Register_JsonStreamEnd_ShouldRegisterAsExpected()
        {
            // Act
            CodecRegistry.Register(CodecType.Json);
            
            // Assert
            ValidateForDefaultJsonCodec<StreamEnd>();
        }
    }
}