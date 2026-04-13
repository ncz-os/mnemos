"""
Unit tests for Hooks module

Tests:
- HookRegistry event dispatcher
- Hook registration/unregistration
- Hook execution and error handling
- History tracking
- Enable/disable functionality
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock

# Note: In actual deployment, these imports would work
# from modules.hooks import HookRegistry, HookEvent, HOOK_SESSION_START


@pytest.mark.asyncio
async def test_hook_registry_creation():
    """Test HookRegistry initialization"""
    from modules.hooks import HookRegistry

    registry = HookRegistry()
    assert registry is not None
    assert len(registry.hooks) == 0
    assert len(registry.history) == 0


@pytest.mark.asyncio
async def test_hook_registration():
    """Test registering hook callbacks"""
    from modules.hooks import HookRegistry

    registry = HookRegistry()

    # Create mock callback
    callback = Mock()

    # Register callback
    registry.register('test.event', callback)

    # Verify registration
    assert 'test.event' in registry.hooks
    assert callback in registry.hooks['test.event']


@pytest.mark.asyncio
async def test_hook_trigger():
    """Test triggering hooks"""
    from modules.hooks import HookRegistry

    registry = HookRegistry({'hooks': {'enabled': True}})

    # Create async callback
    async def async_callback(context):
        context['processed'] = True
        return context

    # Register
    registry.register('test.event', async_callback)

    # Trigger
    context = {'data': 'test'}
    result = await registry.trigger('test.event', context)

    # Verify
    assert result['processed'] is True
    assert result['data'] == 'test'


@pytest.mark.asyncio
async def test_hook_disabled():
    """Test disabled hooks are not triggered"""
    from modules.hooks import HookRegistry

    config = {'hooks': {'enabled': False}}
    registry = HookRegistry(config)

    callback = AsyncMock()
    registry.register('test.event', callback)

    # Trigger should return original context without calling callback
    context = {'data': 'test'}
    result = await registry.trigger('test.event', context)

    # Callback should not be called
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_hook_error_handling():
    """Test error handling in hooks"""
    from modules.hooks import HookRegistry

    registry = HookRegistry({'hooks': {'enabled': True}})

    # Hook that raises error
    async def error_callback(context):
        raise ValueError("Hook error")

    # Normal callback
    async def normal_callback(context):
        context['processed'] = True
        return context

    registry.register('test.event', error_callback)
    registry.register('test.event', normal_callback)

    # Trigger should not crash despite error in first hook
    context = {'data': 'test'}
    result = await registry.trigger('test.event', context)

    # Second hook should still execute
    assert result['processed'] is True


@pytest.mark.asyncio
async def test_hook_history_tracking():
    """Test hook execution history"""
    from modules.hooks import HookRegistry

    registry = HookRegistry({'hooks': {'enabled': True}})

    async def callback(context):
        return context

    registry.register('test.event', callback)

    # Trigger multiple times
    for i in range(3):
        await registry.trigger('test.event', {'iteration': i})

    # Check history
    history = registry.get_history()
    assert len(history) == 3
    assert all(h['event_type'] == 'test.event' for h in history)


@pytest.mark.asyncio
async def test_hook_enable_disable():
    """Test enabling/disabling hooks"""
    from modules.hooks import HookRegistry

    registry = HookRegistry()

    # Initially disabled
    assert not registry.is_enabled('test.event')

    # Enable
    registry.enable_hook('test.event')
    assert registry.is_enabled('test.event')

    # Disable
    registry.disable_hook('test.event')
    assert not registry.is_enabled('test.event')


@pytest.mark.asyncio
async def test_hook_unregistration():
    """Test unregistering hooks"""
    from modules.hooks import HookRegistry

    registry = HookRegistry()

    callback = Mock()
    registry.register('test.event', callback)

    # Verify registered
    assert 'test.event' in registry.hooks
    assert callback in registry.hooks['test.event']

    # Unregister
    registry.unregister('test.event', callback)

    # Verify unregistered
    assert callback not in registry.hooks.get('test.event', [])


@pytest.mark.asyncio
async def test_session_start_hook():
    """Test SessionStartHook functionality"""
    from modules.hooks.session_start import SessionStartHook

    hook = SessionStartHook()
    context = {}

    result = await hook(context)

    # Verify session initialized
    assert 'session_id' in result
    assert 'session_start_time' in result
    assert 'session_metadata' in result
    assert result['session_metadata']['hooks_initialized'] is True


@pytest.mark.asyncio
async def test_prompt_submit_hook():
    """Test PromptSubmitHook task detection"""
    from modules.hooks.prompt_submit import PromptSubmitHook

    hook = PromptSubmitHook()

    # Test code detection
    context = {'prompt': 'Write a Python function to sort arrays'}
    result = await hook(context)
    assert result['detected_task_type'] == 'code_generation'

    # Test architecture detection
    context = {'prompt': 'Design microservices architecture'}
    result = await hook(context)
    assert result['detected_task_type'] == 'architecture_design'

    # Test reasoning detection
    context = {'prompt': 'Why is this approach better?'}
    result = await hook(context)
    assert result['detected_task_type'] == 'reasoning'


@pytest.mark.asyncio
async def test_prompt_tier_selection():
    """Test PromptSubmitHook tier selection"""
    from modules.hooks.prompt_submit import PromptSubmitHook

    hook = PromptSubmitHook()

    # Simple prompt
    context = {'prompt': 'Hi'}
    result = await hook(context)
    assert result['selected_tier_level'] == 4  # Archive

    # Complex prompt
    context = {'prompt': 'How can we optimize? What about? Why is? ' * 20}
    result = await hook(context)
    assert result['selected_tier_level'] == 1  # Hot


def test_prompt_token_estimation():
    """Test token count estimation"""
    from modules.hooks.prompt_submit import PromptSubmitHook

    hook = PromptSubmitHook()

    text = 'This is a test sentence with multiple words'
    tokens = hook._estimate_tokens(text)

    # Should be approximately len(text) / 4
    assert 10 < tokens < 15


def test_task_keywords():
    """Test task detection keywords"""
    from modules.hooks.prompt_submit import PromptSubmitHook

    keywords = PromptSubmitHook.get_task_keywords()

    assert 'code_generation' in keywords
    assert 'code' in keywords['code_generation']
    assert 'reasoning' in keywords
    assert 'architecture' in keywords['reasoning']
