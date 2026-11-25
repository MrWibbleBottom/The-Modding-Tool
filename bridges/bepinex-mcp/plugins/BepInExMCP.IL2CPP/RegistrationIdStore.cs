using System.Collections.Concurrent;
using System.Runtime.CompilerServices;
using Il2CppInterop.Runtime.InteropTypes;

namespace BepInExMCP.IL2CPP;

/// <summary>
/// Shared registration-ID allocator and runtime object handle registry.
/// </summary>
internal sealed class RegistrationIdStore
{
    private readonly ConcurrentDictionary<string, byte> ids =
        new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, object> objects =
        new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<IntPtr, string> il2CppPointers =
        new();
    private readonly ConcurrentDictionary<object, string> managedReferences =
        new(ReferenceEqualityComparer.Instance);
    private long nextObjectSequence;

    internal string Allocate(string? requestedId, string prefix)
    {
        var id = string.IsNullOrWhiteSpace(requestedId)
            ? $"{prefix}-{Guid.NewGuid():N}"
            : requestedId.Trim();

        if (id.Length > 128 ||
            id.Any(character =>
                !(char.IsLetterOrDigit(character) || character is '-' or '_' or '.')))
        {
            throw new ArgumentException(
                "Registration IDs may contain only letters, digits, '.', '_' and '-' " +
                "and must be at most 128 characters.");
        }

        if (!ids.TryAdd(id, 0))
        {
            throw new InvalidOperationException($"Registration ID '{id}' already exists.");
        }

        return id;
    }

    internal string RegisterObject(object instance, string prefix = "obj")
    {
        ArgumentNullException.ThrowIfNull(instance);

        if (instance is Il2CppObjectBase il2CppObject && il2CppObject.Pointer != IntPtr.Zero)
        {
            if (il2CppPointers.TryGetValue(il2CppObject.Pointer, out var existingIl2CppId))
            {
                return existingIl2CppId;
            }
        }
        else if (managedReferences.TryGetValue(instance, out var existingManagedId))
        {
            return existingManagedId;
        }

        var seq = Interlocked.Increment(ref nextObjectSequence);
        var id = Allocate($"{prefix}-{seq}", prefix);
        objects[id] = instance;

        if (instance is Il2CppObjectBase il2Cpp && il2Cpp.Pointer != IntPtr.Zero)
        {
            if (!il2CppPointers.TryAdd(il2Cpp.Pointer, id))
            {
                objects.TryRemove(id, out _);
                ids.TryRemove(id, out _);
                return il2CppPointers[il2Cpp.Pointer];
            }
        }
        else if (!managedReferences.TryAdd(instance, id))
        {
            objects.TryRemove(id, out _);
            ids.TryRemove(id, out _);
            return managedReferences[instance];
        }

        return id;
    }

    internal bool TryGetObject(string objectId, out object? instance)
    {
        if (string.IsNullOrWhiteSpace(objectId))
        {
            instance = null;
            return false;
        }

        return objects.TryGetValue(objectId.Trim(), out instance);
    }

    internal void Release(string id)
    {
        ids.TryRemove(id, out _);
        if (objects.TryRemove(id, out var instance))
        {
            if (instance is Il2CppObjectBase il2Cpp && il2Cpp.Pointer != IntPtr.Zero)
            {
                il2CppPointers.TryRemove(il2Cpp.Pointer, out _);
            }
            else
            {
                managedReferences.TryRemove(instance, out _);
            }
        }
    }

    internal void Clear()
    {
        ids.Clear();
        objects.Clear();
        il2CppPointers.Clear();
        managedReferences.Clear();
    }
}
