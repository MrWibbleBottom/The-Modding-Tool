using System.Collections;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime.InteropTypes;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace BepInExMCP.IL2CPP;

internal sealed class Il2CppGameBackend
{
    private const BindingFlags InstanceMembers =
        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
    private const BindingFlags StaticMembers =
        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;
    private const BindingFlags AllMethods =
        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

    private readonly Il2CppTypeResolver typeResolver;
    private readonly Func<int?> getSelfTestObjectId;
    private readonly RegistrationIdStore registrationIds;

    internal Il2CppGameBackend(
        Il2CppTypeResolver typeResolver,
        Func<int?> getSelfTestObjectId,
        RegistrationIdStore? registrationIds = null)
    {
        this.typeResolver = typeResolver;
        this.getSelfTestObjectId = getSelfTestObjectId;
        this.registrationIds = registrationIds ?? new RegistrationIdStore();
    }

    internal CapabilitiesResponse GetCapabilities()
    {
        var bepinexAssembly = typeof(BasePlugin).Assembly;
        var bepinexVersion = bepinexAssembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()
            ?.InformationalVersion
            ?? bepinexAssembly.GetName().Version?.ToString()
            ?? "unknown";

        return new CapabilitiesResponse(
            Protocol.Version,
            "il2cpp",
            Application.unityVersion,
            bepinexVersion,
            RuntimeInformation.ProcessArchitecture.ToString().ToLowerInvariant(),
            new[]
            {
                "system/capabilities",
                "system/ping",
                "system/paths",
                "scene/list_root_gameobjects",
                "scene/search_gameobjects",
                "scene/hierarchy_snapshot",
                "scene/resolve_selector",
                "gameobject/list_children",
                "gameobject/inspect_components",
                "component/get_details",
                "component/get_member",
                "component/set_value",
                "component/call_method",
                "component/list_methods",
                "type/search",
                "type/describe",
                "type/inspect_static",
                "scene:find_objects_with_component",
                "scene/list_scriptable_objects",
                "object/expand_collection",
                "object/set_collection_entry",
                "network/diagnostics",
                "watch/member",
                "watch/scene",
                "watch/list",
                "watch/remove",
                "mod:subscribe_to_method",
                "mod:patch_method",
                "mod:list_patches",
                "mod:remove_patch",
                "batch",
                "events/webhook"
            },
            new[] { "prefix", "postfix" },
            new[]
            {
                "Members removed by IL2CPP stripping cannot be inspected or invoked.",
                "Obfuscated names must be addressed by their generated interop names.",
                "Harmony transpilers and finalizers are not exposed by this IL2CPP bridge."
            },
            getSelfTestObjectId(),
            new[]
            {
                "stableSelectors",
                "hierarchySnapshots",
                "targetedMemberReads",
                "typeDiscovery",
                "batchPost",
                "memberWatchers",
                "sceneWatchers",
                "patchLifecycle",
                "networkDiagnostics"
            },
            new Dictionary<string, int>
            {
                ["maxBatchOperations"] = Protocol.MaxBatchOperations,
                ["maxRequestBodyBytes"] = Protocol.MaxRequestBodyBytes,
                ["maxWatchers"] = Protocol.MaxWatchers,
                ["maxPatchRegistrations"] = Protocol.MaxPatchRegistrations,
                ["maxSnapshotNodes"] = Protocol.MaxSnapshotNodes
            });
    }

    internal IReadOnlyList<SimpleGameObject> ListRootGameObjects()
    {
        var roots = SceneManager.GetActiveScene().GetRootGameObjects();
        var result = new List<SimpleGameObject>(roots.Length);

        foreach (var gameObject in roots)
        {
            if (gameObject is not null)
            {
                result.Add(ToSimpleObject(gameObject));
            }
        }

        return result;
    }

    internal IReadOnlyList<SimpleGameObject> ListChildren(int parentId)
    {
        var parent = FindObjectById(parentId);
        var transform = parent.transform;
        var result = new List<SimpleGameObject>(transform.childCount);

        for (var index = 0; index < transform.childCount; index++)
        {
            var child = transform.GetChild(index);
            if (child is not null && child.gameObject is not null)
            {
                result.Add(ToSimpleObject(child.gameObject));
            }
        }

        return result;
    }

    internal IReadOnlyList<string> InspectComponents(int gameObjectId)
    {
        var gameObject = FindObjectById(gameObjectId);
        var result = new List<string>();

        foreach (var component in gameObject.GetComponents<Component>())
        {
            if (component is not null)
            {
                result.Add(typeResolver.GetActualTypeName(component));
            }
        }

        return result;
    }

    internal IReadOnlyDictionary<string, ComponentMemberInfo> GetComponentDetails(
        int gameObjectId,
        string componentName)
    {
        var component = ResolveComponent(gameObjectId, componentName);
        var details = new SortedDictionary<string, ComponentMemberInfo>(StringComparer.Ordinal);

        foreach (var field in component.Type.GetFields(InstanceMembers))
        {
            try
            {
                var value = field.GetValue(component.Instance);
                details[field.Name] = new ComponentMemberInfo(
                    FormatType(field.FieldType),
                    ValueConverter.SafeToString(value));
            }
            catch
            {
                // Some generated or stripped field accessors cannot be read safely.
            }
        }

        foreach (var property in component.Type.GetProperties(InstanceMembers))
        {
            if (!property.CanRead || property.GetIndexParameters().Length != 0)
            {
                continue;
            }

            try
            {
                var value = property.GetValue(component.Instance);
                details[property.Name] = new ComponentMemberInfo(
                    FormatType(property.PropertyType),
                    ValueConverter.SafeToString(value));
            }
            catch
            {
                // Properties can contain game logic or represent stripped IL2CPP fields.
            }
        }

        return details;
    }

    internal StatusResponse SetComponentValue(
        int gameObjectId,
        string componentName,
        string memberName,
        string value)
    {
        var component = ResolveComponent(gameObjectId, componentName);
        var (targetInstance, targetType, leafMemberName) = TraverseMemberPath(
            component.Instance,
            component.Type,
            memberName);

        var field = targetType.GetField(
            leafMemberName,
            InstanceMembers | BindingFlags.IgnoreCase);
        if (field is not null)
        {
            var converted = ValueConverter.ConvertString(value, field.FieldType);
            field.SetValue(targetInstance, converted);
            return new StatusResponse(
                "ok",
                $"Set field '{field.Name}' on '{FormatType(targetType)}'.");
        }

        var property = targetType.GetProperty(
            leafMemberName,
            InstanceMembers | BindingFlags.IgnoreCase);
        if (property is null)
        {
            throw new MissingMemberException(
                targetType.FullName,
                leafMemberName);
        }

        if (!property.CanWrite || property.GetIndexParameters().Length != 0)
        {
            throw new InvalidOperationException($"Property '{property.Name}' is read-only.");
        }

        var propertyValue = ValueConverter.ConvertString(value, property.PropertyType);
        property.SetValue(targetInstance, propertyValue);
        return new StatusResponse(
            "ok",
            $"Set property '{property.Name}' on '{FormatType(targetType)}'.");
    }

    internal StatusResponse CallComponentMethod(
        int gameObjectId,
        string componentName,
        string methodName,
        string argumentsJson)
    {
        var component = ResolveComponent(gameObjectId, componentName);
        var (targetInstance, targetType, leafMethodName) = TraverseMemberPath(
            component.Instance,
            component.Type,
            methodName);
        var arguments = ValueConverter.ParseArguments(argumentsJson);
        var methodAndArguments = SelectInvocableMethod(
            targetType,
            leafMethodName,
            arguments);

        object? returnValue;
        try
        {
            returnValue = methodAndArguments.Method.Invoke(
                targetInstance,
                methodAndArguments.Arguments);
        }
        catch (TargetInvocationException exception) when (exception.InnerException is not null)
        {
            throw new InvalidOperationException(
                $"Method '{methodAndArguments.Method.Name}' threw: " +
                exception.InnerException.Message,
                exception.InnerException);
        }

        return new StatusResponse(
            "ok",
            $"Called '{FormatMethod(methodAndArguments.Method)}'. " +
            $"ReturnValue: {ValueConverter.SafeToString(returnValue)}");
    }

    internal IReadOnlyList<string> ListComponentMethods(
        int gameObjectId,
        string componentName)
    {
        var component = ResolveComponent(gameObjectId, componentName);
        return component.Type
            .GetMethods(InstanceMembers)
            .Where(method => !method.IsSpecialName)
            .Select(FormatMethod)
            .OrderBy(signature => signature, StringComparer.Ordinal)
            .ToArray();
    }

    internal IReadOnlyList<SimpleGameObject> FindObjectsWithComponent(string componentName)
    {
        var requestedType = typeResolver.ResolveRequired(componentName);
        var result = new List<SimpleGameObject>();
        var seen = new HashSet<int>();

        foreach (var gameObject in Resources.FindObjectsOfTypeAll<GameObject>())
        {
            if (gameObject is null ||
                (!gameObject.scene.IsValid() && gameObject.hideFlags == HideFlags.None))
            {
                continue;
            }

            foreach (var component in gameObject.GetComponents<Component>())
            {
                if (component is null)
                {
                    continue;
                }

                var actualType = typeResolver.GetActualType(component);
                if (actualType != requestedType && !requestedType.IsAssignableFrom(actualType))
                {
                    continue;
                }

                var id = gameObject.GetInstanceID();
                if (seen.Add(id))
                {
                    result.Add(ToSimpleObject(gameObject));
                }

                break;
            }
        }

        return result;
    }

    internal SearchResponse SearchGameObjects(
        string name,
        string componentName,
        string tag,
        string sceneName,
        bool includeInactive,
        int limit)
    {
        limit = Math.Clamp(limit, 1, 1_000);
        var result = new List<GameObjectSearchResult>(Math.Min(limit, 128));
        var truncated = false;

        foreach (var gameObject in Resources.FindObjectsOfTypeAll<GameObject>())
        {
            if (gameObject is null ||
                (!gameObject.scene.IsValid() && gameObject.hideFlags == HideFlags.None))
            {
                continue;
            }

            if (!includeInactive && !gameObject.activeInHierarchy)
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(name) &&
                !gameObject.name.Contains(name, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(tag) &&
                !string.Equals(gameObject.tag, tag, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(sceneName) &&
                !string.Equals(
                    GetSceneName(gameObject),
                    sceneName,
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(componentName) &&
                !HasComponent(gameObject, componentName))
            {
                continue;
            }

            if (result.Count == limit)
            {
                truncated = true;
                break;
            }

            result.Add(ToSearchResult(gameObject));
        }

        return new SearchResponse(result, truncated);
    }

    internal IReadOnlyList<HierarchyNode> GetHierarchySnapshot(
        int? rootId,
        int depth,
        int maxNodes)
    {
        depth = Math.Clamp(depth, 0, 16);
        maxNodes = Math.Clamp(maxNodes, 1, Protocol.MaxSnapshotNodes);
        var remaining = maxNodes;
        var result = new List<HierarchyNode>();

        if (rootId.HasValue)
        {
            result.Add(BuildHierarchyNode(FindObjectById(rootId.Value), depth, ref remaining));
            return result;
        }

        foreach (var root in SceneManager.GetActiveScene().GetRootGameObjects())
        {
            if (root is null || remaining <= 0)
            {
                break;
            }

            result.Add(BuildHierarchyNode(root, depth, ref remaining));
        }

        return result;
    }

    internal ObjectSelector GetSelector(int instanceId)
    {
        var gameObject = FindObjectById(instanceId);
        return new ObjectSelector(
            GetSceneName(gameObject),
            GetObjectPath(gameObject),
            null,
            gameObject.name);
    }

    internal GameObjectSearchResult ResolveSelector(ObjectSelector selector)
    {
        ArgumentNullException.ThrowIfNull(selector);
        var matches = new List<GameObject>();

        foreach (var gameObject in Resources.FindObjectsOfTypeAll<GameObject>())
        {
            if (gameObject is null ||
                (!gameObject.scene.IsValid() && gameObject.hideFlags == HideFlags.None))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(selector.Scene) &&
                !string.Equals(
                    GetSceneName(gameObject),
                    selector.Scene,
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(selector.Path) &&
                !string.Equals(GetObjectPath(gameObject), selector.Path, StringComparison.Ordinal))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(selector.Name) &&
                !string.Equals(gameObject.name, selector.Name, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (!string.IsNullOrWhiteSpace(selector.Component) &&
                !HasComponent(gameObject, selector.Component))
            {
                continue;
            }

            matches.Add(gameObject);
            if (matches.Count > 1)
            {
                break;
            }
        }

        return matches.Count switch
        {
            1 => ToSearchResult(matches[0]),
            0 => throw new KeyNotFoundException("No GameObject matched the supplied selector."),
            _ => throw new AmbiguousMatchException(
                "The selector matched more than one GameObject. Add a scene or hierarchy path.")
        };
    }

    internal MemberValueResponse GetComponentMember(
        int gameObjectId,
        string componentName,
        string memberName)
    {
        var component = ResolveComponent(gameObjectId, componentName);
        return ReadMember(component, memberName);
    }

    internal IReadOnlyList<TypeSummary> SearchTypes(string query, int offset, int limit) =>
        typeResolver.Search(query, offset, limit)
            .Select(ToTypeSummary)
            .ToArray();

    internal TypeDescription DescribeType(string typeName, int offset, int limit)
    {
        var type = typeResolver.ResolveRequired(typeName);
        offset = Math.Max(0, offset);
        limit = Math.Clamp(limit, 1, 500);

        var members = type
            .GetFields(AllMethods)
            .Select(field => new MemberDescription(
                field.Name,
                FormatType(field.FieldType),
                "field",
                true,
                !field.IsInitOnly && !field.IsLiteral,
                field.IsStatic))
            .Concat(type.GetProperties(AllMethods)
                .Where(property => property.GetIndexParameters().Length == 0)
                .Select(property => new MemberDescription(
                    property.Name,
                    FormatType(property.PropertyType),
                    "property",
                    property.CanRead,
                    property.CanWrite,
                    (property.GetMethod ?? property.SetMethod)?.IsStatic ?? false)))
            .OrderBy(member => member.Name, StringComparer.Ordinal)
            .Skip(offset)
            .Take(limit)
            .ToArray();

        var methods = type
            .GetMethods(AllMethods)
            .Where(method => !method.IsSpecialName)
            .OrderBy(method => method.Name, StringComparer.Ordinal)
            .ThenBy(method => method.GetParameters().Length)
            .Skip(offset)
            .Take(limit)
            .Select(method => new MethodDescription(
                method.Name,
                FormatMethod(method),
                FormatType(method.ReturnType),
                method.GetParameters()
                    .Select(parameter => FormatType(parameter.ParameterType))
                    .ToArray(),
                method.IsStatic))
            .ToArray();

        return new TypeDescription(ToTypeSummary(type), members, methods);
    }

    internal IReadOnlyList<ScriptableObjectSummary> ListScriptableObjects(
        string filter = "",
        int limit = 100)
    {
        limit = Math.Clamp(limit, 1, 500);
        var normalizedFilter = filter?.Trim() ?? string.Empty;
        var result = new List<ScriptableObjectSummary>(Math.Min(limit, 100));

        foreach (var scriptableObject in Resources.FindObjectsOfTypeAll<ScriptableObject>())
        {
            if (scriptableObject is null)
            {
                continue;
            }

            var actualType = typeResolver.GetActualType(scriptableObject);
            var fullTypeName = FormatType(actualType);
            var objectName = scriptableObject.name ?? string.Empty;

            if (IsInternalAsset(objectName, fullTypeName))
            {
                continue;
            }

            if (!string.IsNullOrEmpty(normalizedFilter) &&
                !objectName.Contains(normalizedFilter, StringComparison.OrdinalIgnoreCase) &&
                !fullTypeName.Contains(normalizedFilter, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var typedInstance = CastIfIl2Cpp(scriptableObject, actualType);
            var objectId = registrationIds.RegisterObject(typedInstance, "so");
            result.Add(new ScriptableObjectSummary(objectId, objectName, fullTypeName));

            if (result.Count >= limit)
            {
                break;
            }
        }

        return result;
    }

    internal StaticTypeInspection InspectStaticType(string typeName)
    {
        var type = typeResolver.ResolveRequired(typeName);
        var staticMembers = new List<StaticMemberInfo>();

        foreach (var field in type.GetFields(StaticMembers))
        {
            if (IsIl2CppInteropMetadataField(field))
            {
                continue;
            }

            try
            {
                var rawValue = field.GetValue(null);
                var objectId = MaybeRegisterReferenceObject(rawValue, "ref");
                staticMembers.Add(new StaticMemberInfo(
                    field.Name,
                    FormatType(field.FieldType),
                    ValueConverter.SafeToString(rawValue),
                    objectId));
            }
            catch (Exception exception)
            {
                staticMembers.Add(new StaticMemberInfo(
                    field.Name,
                    FormatType(field.FieldType),
                    $"<error: {exception.Message}>"));
            }
        }

        foreach (var property in type.GetProperties(StaticMembers))
        {
            if (!property.CanRead || property.GetIndexParameters().Length != 0)
            {
                continue;
            }

            try
            {
                var rawValue = property.GetValue(null);
                var objectId = MaybeRegisterReferenceObject(rawValue, "ref");
                staticMembers.Add(new StaticMemberInfo(
                    property.Name,
                    FormatType(property.PropertyType),
                    ValueConverter.SafeToString(rawValue),
                    objectId));
            }
            catch (Exception exception)
            {
                staticMembers.Add(new StaticMemberInfo(
                    property.Name,
                    FormatType(property.PropertyType),
                    $"<error: {exception.Message}>"));
            }
        }

        staticMembers.Sort((a, b) => StringComparer.Ordinal.Compare(a.Name, b.Name));
        return new StaticTypeInspection(FormatType(type), staticMembers);
    }

    internal CollectionExpansionResponse ExpandCollection(
        string objectId,
        string fieldPath = "",
        int limit = 50)
    {
        limit = Math.Clamp(limit, 1, 500);
        var (collection, collectionType, normalizedPath) = ResolveCollectionTarget(objectId, fieldPath);

        if (collection is Il2CppObjectBase il2CppCollection)
        {
            var actualType = typeResolver.GetActualType(il2CppCollection);
            collection = CastIfIl2Cpp(il2CppCollection, actualType);
            collectionType = GetActualRuntimeType(collection);
        }

        var entries = new List<CollectionEntry>(Math.Min(limit, 64));
        int totalCount;
        bool truncated;

        if (collection is Array managedArray)
        {
            totalCount = managedArray.Length;
            var elemHint = collectionType.GetElementType() ?? typeof(object);
            var take = Math.Min(totalCount, limit);
            for (var i = 0; i < take; i++)
            {
                entries.Add(CreateCollectionEntry(
                    i.ToString(CultureInfo.InvariantCulture),
                    managedArray.GetValue(i),
                    elemHint));
            }
            truncated = totalCount > limit;
        }
        else if (collection is IDictionary managedDict)
        {
            totalCount = managedDict.Count;
            var genericArgs = collectionType.GetGenericArguments();
            var valueHint = genericArgs.Length >= 2 ? genericArgs[1] : typeof(object);
            var enumerator = managedDict.GetEnumerator();
            try
            {
                while (enumerator.MoveNext() && entries.Count < limit)
                {
                    entries.Add(CreateCollectionEntry(
                        ValueConverter.SafeToString(enumerator.Key),
                        enumerator.Value,
                        valueHint));
                }
            }
            finally
            {
                if (enumerator is IDisposable disposable)
                {
                    disposable.Dispose();
                }
            }
            truncated = totalCount > entries.Count;
        }
        else if (collection is IList managedList)
        {
            totalCount = managedList.Count;
            var genericArgs = collectionType.GetGenericArguments();
            var elemHint = genericArgs.Length >= 1 ? genericArgs[0] : typeof(object);
            var take = Math.Min(totalCount, limit);
            for (var i = 0; i < take; i++)
            {
                entries.Add(CreateCollectionEntry(
                    i.ToString(CultureInfo.InvariantCulture),
                    managedList[i],
                    elemHint));
            }
            truncated = totalCount > limit;
        }
        else if (TryExpandIl2CppDictionary(collection, collectionType, limit, entries, out totalCount, out truncated))
        {
            // Populated via IL2CPP dictionary reflection.
        }
        else if (TryExpandIl2CppIndexedCollection(collection, collectionType, limit, entries, out totalCount, out truncated))
        {
            // Populated via IL2CPP List<T> / Il2CppArrayBase<T> reflection.
        }
        else if (collection is IEnumerable enumerable)
        {
            totalCount = ReadOptionalCount(collection, collectionType) ?? 0;
            var genericArgs = collectionType.GetGenericArguments();
            var elemHint = genericArgs.Length >= 1 ? genericArgs[0] : typeof(object);
            var index = 0;
            var enumerator = enumerable.GetEnumerator();
            try
            {
                while (enumerator.MoveNext())
                {
                    if (entries.Count < limit)
                    {
                        entries.Add(CreateCollectionEntry(
                            index.ToString(CultureInfo.InvariantCulture),
                            enumerator.Current,
                            elemHint));
                    }
                    else
                    {
                        truncated = true;
                        break;
                    }
                    index++;
                }
                truncated = entries.Count >= limit && enumerator.MoveNext();
            }
            finally
            {
                if (enumerator is IDisposable disposable)
                {
                    disposable.Dispose();
                }
            }

            if (totalCount == 0 && entries.Count > 0)
            {
                totalCount = entries.Count + (truncated ? 1 : 0);
            }
        }
        else
        {
            throw new InvalidOperationException(
                $"Target member '{normalizedPath}' of type '{FormatType(collectionType)}' is not a supported collection.");
        }

        return new CollectionExpansionResponse(
            objectId,
            normalizedPath,
            FormatType(collectionType),
            totalCount,
            truncated,
            entries);
    }

    internal StatusResponse SetCollectionEntry(
        string objectId,
        string fieldPath,
        string key,
        string value)
    {
        var (collection, collectionType, normalizedPath) = ResolveCollectionTarget(objectId, fieldPath);

        if (collection is Il2CppObjectBase il2CppCollection)
        {
            var actualType = typeResolver.GetActualType(il2CppCollection);
            collection = CastIfIl2Cpp(il2CppCollection, actualType);
            collectionType = GetActualRuntimeType(collection);
        }

        if (collection is Array managedArray)
        {
            var index = ParseCollectionIndex(key, managedArray.Length);
            var elementType = collectionType.GetElementType() ?? typeof(object);
            EnsureEditableCollectionElementType(elementType);
            var converted = ValueConverter.ConvertString(value, elementType);
            managedArray.SetValue(converted, index);
            return new StatusResponse(
                "ok",
                $"Set array index [{index}] on '{normalizedPath}' ({FormatType(elementType)}).");
        }

        if (collection is IDictionary managedDict)
        {
            var genericArgs = collectionType.GetGenericArguments();
            var keyType = genericArgs.Length >= 1 ? genericArgs[0] : typeof(string);
            var valueType = genericArgs.Length >= 2 ? genericArgs[1] : typeof(object);
            EnsureEditableCollectionElementType(valueType);
            var convertedKey = ConvertDictionaryKey(managedDict, keyType, key);
            var convertedValue = ValueConverter.ConvertString(value, valueType);
            managedDict[convertedKey] = convertedValue;
            return new StatusResponse(
                "ok",
                $"Set dictionary entry '{key}' on '{normalizedPath}'.");
        }

        if (collection is IList managedList)
        {
            var index = ParseCollectionIndex(key, managedList.Count);
            var genericArgs = collectionType.GetGenericArguments();
            var existing = managedList[index];
            var elementType = genericArgs.Length >= 1
                ? genericArgs[0]
                : (existing is not null ? GetActualRuntimeType(existing) : typeof(object));
            EnsureEditableCollectionElementType(elementType);
            var converted = ValueConverter.ConvertString(value, elementType);
            managedList[index] = converted;
            return new StatusResponse(
                "ok",
                $"Set list index [{index}] on '{normalizedPath}' ({FormatType(elementType)}).");
        }

        if (TrySetIl2CppDictionaryEntry(collection, collectionType, key, value, normalizedPath, out var dictStatus))
        {
            return dictStatus;
        }

        if (TrySetIl2CppIndexedEntry(collection, collectionType, key, value, normalizedPath, out var listStatus))
        {
            return listStatus;
        }

        throw new InvalidOperationException(
            $"Target member '{normalizedPath}' of type '{FormatType(collectionType)}' does not support writing collection entries.");
    }

    internal NetworkDiagnosticsResponse GetNetworkDiagnostics(int gameObjectId)
    {
        var gameObject = FindObjectById(gameObjectId);
        var diagnostics = new List<NetworkObjectDiagnostic>();
        var propertyNames = new[]
        {
            "IsOwner", "HasAuthority", "IsServer", "IsClient", "IsHost",
            "IsSpawned", "OwnerId", "ObjectId", "NetworkObjectId", "ViewID"
        };

        foreach (var component in gameObject.GetComponents<Component>())
        {
            if (component is null)
            {
                continue;
            }

            var type = typeResolver.GetActualType(component);
            var typed = typeResolver.Cast(component, type);
            var values = new SortedDictionary<string, string>(StringComparer.Ordinal);

            foreach (var propertyName in propertyNames)
            {
                var property = type.GetProperty(propertyName, InstanceMembers);
                if (property is null || !property.CanRead || property.GetIndexParameters().Length != 0)
                {
                    continue;
                }

                try
                {
                    values[propertyName] = ValueConverter.SafeToString(property.GetValue(typed));
                }
                catch
                {
                }
            }

            if (values.Count == 0)
            {
                continue;
            }

            diagnostics.Add(new NetworkObjectDiagnostic(
                FormatType(type),
                DetectNetworkFramework(type),
                values));
        }

        return new NetworkDiagnosticsResponse(gameObjectId, diagnostics);
    }

    internal string ReadMemberValue(ObjectSelector selector, string componentName, string memberName)
    {
        var target = ResolveSelector(selector);
        return GetComponentMember(target.Id, componentName, memberName).Value;
    }

    internal ResolvedComponent ResolveComponent(int gameObjectId, string componentName)
    {
        var gameObject = FindObjectById(gameObjectId);
        var exactMatches = new List<(Component Component, Type Type)>();
        var simpleMatches = new List<(Component Component, Type Type)>();

        foreach (var component in gameObject.GetComponents<Component>())
        {
            if (component is null)
            {
                continue;
            }

            var actualType = typeResolver.GetActualType(component);
            if (string.Equals(actualType.FullName, componentName, StringComparison.Ordinal) ||
                string.Equals(
                    actualType.AssemblyQualifiedName,
                    componentName,
                    StringComparison.Ordinal))
            {
                exactMatches.Add((component, actualType));
            }
            else if (string.Equals(actualType.Name, componentName, StringComparison.Ordinal) ||
                     string.Equals(
                         actualType.FullName,
                         componentName,
                         StringComparison.OrdinalIgnoreCase))
            {
                simpleMatches.Add((component, actualType));
            }
        }

        var matches = exactMatches.Count > 0 ? exactMatches : simpleMatches;
        if (matches.Count == 0)
        {
            throw new KeyNotFoundException(
                $"Component '{componentName}' was not found on GameObject {gameObjectId}.");
        }

        var distinctTypes = matches
            .Select(match => match.Type)
            .Distinct()
            .ToArray();
        if (distinctTypes.Length > 1)
        {
            throw new AmbiguousMatchException(
                $"Component name '{componentName}' is ambiguous on GameObject {gameObjectId}: " +
                string.Join(", ", distinctTypes.Select(FormatType)));
        }

        var selected = matches[0];
        var typedInstance = typeResolver.Cast(selected.Component, selected.Type);
        return new ResolvedComponent(gameObject, selected.Component, typedInstance, selected.Type);
    }

    internal Type ResolveType(string typeName) => typeResolver.ResolveRequired(typeName);

    internal MethodInfo ResolveMethod(
        Type targetType,
        string methodName,
        string parameterTypeNames)
    {
        var methods = targetType
            .GetMethods(AllMethods)
            .Where(method =>
                !method.IsGenericMethodDefinition &&
                string.Equals(method.Name, methodName, StringComparison.Ordinal))
            .ToArray();

        if (!string.IsNullOrWhiteSpace(parameterTypeNames))
        {
            var parameterTypes = parameterTypeNames
                .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(typeResolver.ResolveRequired)
                .ToArray();
            methods = methods
                .Where(method => method
                    .GetParameters()
                    .Select(parameter => parameter.ParameterType)
                    .SequenceEqual(parameterTypes))
                .ToArray();
        }

        return methods.Length switch
        {
            1 => methods[0],
            0 => throw new MissingMethodException(
                targetType.FullName,
                $"{methodName}({parameterTypeNames})"),
            _ => throw new AmbiguousMatchException(
                $"Method '{targetType.FullName}.{methodName}' is overloaded. " +
                "Supply parameterTypes. Matches: " +
                string.Join("; ", methods.Select(FormatMethod)))
        };
    }

    internal GameObject FindObjectById(int instanceId)
    {
        foreach (var gameObject in Resources.FindObjectsOfTypeAll<GameObject>())
        {
            if (gameObject is not null && gameObject.GetInstanceID() == instanceId)
            {
                return gameObject;
            }
        }

        throw new KeyNotFoundException($"GameObject with ID {instanceId} was not found.");
    }

    private static SelectedMethod SelectInvocableMethod(
        Type componentType,
        string methodName,
        IReadOnlyList<System.Text.Json.JsonElement> arguments)
    {
        var candidates = componentType
            .GetMethods(InstanceMembers)
            .Where(method =>
                !method.IsSpecialName &&
                !method.IsGenericMethodDefinition &&
                string.Equals(method.Name, methodName, StringComparison.OrdinalIgnoreCase) &&
                method.GetParameters().Length == arguments.Count)
            .ToArray();

        var matches = new List<SelectedMethod>();
        foreach (var candidate in candidates)
        {
            var parameters = candidate.GetParameters();
            var converted = new object?[parameters.Length];
            var valid = true;

            for (var index = 0; index < parameters.Length; index++)
            {
                try
                {
                    converted[index] = ValueConverter.ConvertJson(
                        arguments[index],
                        parameters[index].ParameterType);
                }
                catch
                {
                    valid = false;
                    break;
                }
            }

            if (valid)
            {
                matches.Add(new SelectedMethod(candidate, converted));
            }
        }

        return matches.Count switch
        {
            1 => matches[0],
            0 => throw new MissingMethodException(
                $"No overload of '{componentType.FullName}.{methodName}' accepts " +
                $"{arguments.Count} supplied argument(s)."),
            _ => throw new AmbiguousMatchException(
                $"More than one overload of '{componentType.FullName}.{methodName}' accepts " +
                "the supplied arguments. Matches: " +
                string.Join("; ", matches.Select(match => FormatMethod(match.Method))))
        };
    }

    private static SimpleGameObject ToSimpleObject(GameObject gameObject) =>
        new(gameObject.name, gameObject.GetInstanceID());

    private bool HasComponent(GameObject gameObject, string componentName)
    {
        foreach (var component in gameObject.GetComponents<Component>())
        {
            if (component is null)
            {
                continue;
            }

            var type = typeResolver.GetActualType(component);
            if (string.Equals(type.FullName, componentName, StringComparison.Ordinal) ||
                string.Equals(type.Name, componentName, StringComparison.Ordinal) ||
                string.Equals(type.FullName, componentName, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    private HierarchyNode BuildHierarchyNode(GameObject gameObject, int depth, ref int remaining)
    {
        remaining--;
        var children = new List<HierarchyNode>();

        if (depth > 0 && remaining > 0)
        {
            var transform = gameObject.transform;
            for (var index = 0; index < transform.childCount && remaining > 0; index++)
            {
                var child = transform.GetChild(index);
                if (child is not null && child.gameObject is not null)
                {
                    children.Add(BuildHierarchyNode(child.gameObject, depth - 1, ref remaining));
                }
            }
        }

        return new HierarchyNode(
            gameObject.name,
            gameObject.GetInstanceID(),
            GetSceneName(gameObject),
            GetObjectPath(gameObject),
            gameObject.activeInHierarchy,
            children);
    }

    private static string GetObjectPath(GameObject gameObject)
    {
        var segments = new Stack<string>();
        var current = gameObject.transform;

        while (current is not null)
        {
            segments.Push(
                $"{Uri.EscapeDataString(current.gameObject.name)}[{current.GetSiblingIndex()}]");
            current = current.parent;
        }

        return "/" + string.Join("/", segments);
    }

    private static GameObjectSearchResult ToSearchResult(GameObject gameObject) =>
        new(
            gameObject.name,
            gameObject.GetInstanceID(),
            GetSceneName(gameObject),
            GetObjectPath(gameObject),
            gameObject.activeInHierarchy);

    private static string GetSceneName(GameObject gameObject) =>
        gameObject.scene.IsValid() && !string.IsNullOrEmpty(gameObject.scene.name)
            ? gameObject.scene.name
            : "<persistent>";

    private static (object Instance, Type Type, string LeafName) TraverseMemberPath(
        object rootInstance,
        Type rootType,
        string memberPath)
    {
        var segments = memberPath.Split('.', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (segments.Length <= 1)
        {
            return (rootInstance, rootType, memberPath.Trim());
        }

        var currentInstance = rootInstance;
        var currentType = rootType;

        for (var i = 0; i < segments.Length - 1; i++)
        {
            var seg = segments[i];
            object? nextInstance = null;
            Type? nextType = null;

            var field = currentType.GetField(seg, InstanceMembers | BindingFlags.IgnoreCase);
            if (field is not null)
            {
                nextInstance = field.GetValue(currentInstance);
                nextType = field.FieldType;
            }
            else
            {
                var prop = currentType.GetProperty(seg, InstanceMembers | BindingFlags.IgnoreCase);
                if (prop is not null && prop.CanRead && prop.GetIndexParameters().Length == 0)
                {
                    nextInstance = prop.GetValue(currentInstance);
                    nextType = prop.PropertyType;
                }
                else
                {
                    // Support StatsFacade.GetStatObject(Stats.Type) by stat enum name (e.g. Health, ActionPoints, MoveRange, AttackDamage)
                    var getStatObj = currentType.GetMethod("GetStatObject", InstanceMembers | BindingFlags.IgnoreCase);
                    if (getStatObj is not null && getStatObj.GetParameters().Length == 1)
                    {
                        var enumParamType = getStatObj.GetParameters()[0].ParameterType;
                        if (enumParamType.IsEnum && Enum.TryParse(enumParamType, seg, ignoreCase: true, out var enumVal))
                        {
                            nextInstance = getStatObj.Invoke(currentInstance, new[] { enumVal });
                            nextType = getStatObj.ReturnType;
                        }
                        else
                        {
                            throw new MissingMemberException(currentType.FullName, seg);
                        }
                    }
                    else
                    {
                        throw new MissingMemberException(currentType.FullName, seg);
                    }
                }
            }

            if (nextInstance is null)
            {
                throw new InvalidOperationException(
                    $"Intermediate member '{seg}' on '{FormatType(currentType)}' evaluated to null.");
            }

            currentInstance = nextInstance;
            currentType = nextInstance.GetType() != typeof(object) ? nextInstance.GetType() : (nextType ?? nextInstance.GetType());
        }

        return (currentInstance, currentType, segments[^1]);
    }

    private static MemberValueResponse ReadMember(
        ResolvedComponent component,
        string memberName)
    {
        var (targetInstance, targetType, leafMemberName) = TraverseMemberPath(
            component.Instance,
            component.Type,
            memberName);

        var field = targetType.GetField(
            leafMemberName,
            InstanceMembers | BindingFlags.IgnoreCase);
        if (field is not null)
        {
            return new MemberValueResponse(
                memberName,
                FormatType(field.FieldType),
                ValueConverter.SafeToString(field.GetValue(targetInstance)),
                !field.IsInitOnly && !field.IsLiteral,
                "field");
        }

        var property = targetType.GetProperty(
            leafMemberName,
            InstanceMembers | BindingFlags.IgnoreCase);
        if (property is null || property.GetIndexParameters().Length != 0)
        {
            throw new MissingMemberException(targetType.FullName, leafMemberName);
        }

        if (!property.CanRead)
        {
            throw new InvalidOperationException($"Property '{property.Name}' is write-only.");
        }

        return new MemberValueResponse(
            memberName,
            FormatType(property.PropertyType),
            ValueConverter.SafeToString(property.GetValue(targetInstance)),
            property.CanWrite,
            "property");
    }

    private static TypeSummary ToTypeSummary(Type type) =>
        new(
            type.Name,
            FormatType(type),
            type.Assembly.GetName().Name ?? "unknown",
            typeof(Component).IsAssignableFrom(type));

    private static string? DetectNetworkFramework(Type type)
    {
        var name = type.FullName ?? type.Name;
        if (name.Contains("FishNet.", StringComparison.Ordinal))
        {
            return "FishNet";
        }

        if (name.Contains("Mirror.", StringComparison.Ordinal))
        {
            return "Mirror";
        }

        if (name.Contains("Unity.Netcode.", StringComparison.Ordinal))
        {
            return "Unity.Netcode";
        }

        if (name.Contains("Photon.", StringComparison.Ordinal))
        {
            return "Photon";
        }

        return null;
    }

    private static bool IsInternalAsset(string name, string fullTypeName) =>
        fullTypeName.StartsWith("UnityEngine.", StringComparison.Ordinal) ||
        fullTypeName.StartsWith("UnityEditor.", StringComparison.Ordinal) ||
        fullTypeName.StartsWith("Unity.", StringComparison.Ordinal) ||
        fullTypeName.StartsWith("TMPro.", StringComparison.Ordinal) ||
        fullTypeName.StartsWith("Il2CppTMPro.", StringComparison.Ordinal) ||
        name.StartsWith("TMP ", StringComparison.OrdinalIgnoreCase) ||
        name.StartsWith("LiberationSans", StringComparison.OrdinalIgnoreCase);

    private static bool IsIl2CppInteropMetadataField(FieldInfo field) =>
        field.FieldType == typeof(IntPtr) &&
        (field.Name.StartsWith("NativeFieldInfoPtr_", StringComparison.Ordinal) ||
         field.Name.StartsWith("NativeMethodInfoPtr_", StringComparison.Ordinal));

    private object CastIfIl2Cpp(object instance, Type targetType)
    {
        if (instance is Il2CppObjectBase il2Cpp && typeof(Il2CppObjectBase).IsAssignableFrom(targetType))
        {
            try
            {
                return typeResolver.Cast(il2Cpp, targetType);
            }
            catch
            {
                return instance;
            }
        }

        return instance;
    }

    private Type GetActualRuntimeType(object instance)
    {
        if (instance is Il2CppObjectBase il2Cpp)
        {
            return typeResolver.GetActualType(il2Cpp);
        }

        return instance.GetType();
    }

    private string? MaybeRegisterReferenceObject(object? rawValue, string prefix)
    {
        if (rawValue is null || rawValue is string)
        {
            return null;
        }

        var actualType = GetActualRuntimeType(rawValue);
        if (actualType.IsValueType || actualType.IsEnum)
        {
            return null;
        }

        var typed = CastIfIl2Cpp(rawValue, actualType);
        return registrationIds.RegisterObject(typed, prefix);
    }

    private CollectionEntry CreateCollectionEntry(string key, object? element, Type elementHint)
    {
        var elementType = element is not null ? GetActualRuntimeType(element) : elementHint;
        var objectId = MaybeRegisterReferenceObject(element, "elem");
        return new CollectionEntry(
            key,
            ValueConverter.SafeToString(element),
            FormatType(elementType),
            objectId);
    }

    private (object Collection, Type CollectionType, string NormalizedPath) ResolveCollectionTarget(
        string objectId,
        string fieldPath)
    {
        var rootObject = ResolveRegisteredOrUnityObject(objectId);
        var trimmedPath = fieldPath?.Trim() ?? string.Empty;
        if (string.IsNullOrEmpty(trimmedPath))
        {
            var rootType = GetActualRuntimeType(rootObject);
            return (CastIfIl2Cpp(rootObject, rootType), rootType, string.Empty);
        }

        var resolvedRoot = CastIfIl2Cpp(rootObject, GetActualRuntimeType(rootObject));
        var resolvedRootType = GetActualRuntimeType(resolvedRoot);

        // If objectId refers to a GameObject and fieldPath starts with a Component name (e.g., "Inventory.items")
        if (resolvedRoot is GameObject gameObject)
        {
            var dotIdx = trimmedPath.IndexOf('.');
            if (dotIdx > 0)
            {
                var compName = trimmedPath[..dotIdx];
                var memberSubPath = trimmedPath[(dotIdx + 1)..];
                try
                {
                    var component = ResolveComponent(gameObject.GetInstanceID(), compName);
                    var (compTarget, compTargetType, compLeaf) = TraverseMemberPath(
                        component.Instance,
                        component.Type,
                        memberSubPath);
                    var compVal = ReadDirectMemberValue(compTarget, compTargetType, compLeaf);
                    var compValType = GetActualRuntimeType(compVal);
                    return (CastIfIl2Cpp(compVal, compValType), compValType, trimmedPath);
                }
                catch (KeyNotFoundException)
                {
                    // Fall through to direct GameObject member traversal.
                }
            }
        }

        var (targetInstance, targetType, leafMemberName) = TraverseMemberPath(
            resolvedRoot,
            resolvedRootType,
            trimmedPath);
        var collection = ReadDirectMemberValue(targetInstance, targetType, leafMemberName);
        var collectionType = GetActualRuntimeType(collection);
        return (CastIfIl2Cpp(collection, collectionType), collectionType, trimmedPath);
    }

    private object ResolveRegisteredOrUnityObject(string objectId)
    {
        if (string.IsNullOrWhiteSpace(objectId))
        {
            throw new ArgumentException("Query parameter 'objectId' is required.");
        }

        if (registrationIds.TryGetObject(objectId, out var registered) && registered is not null)
        {
            return registered;
        }

        if (int.TryParse(objectId.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var instanceId))
        {
            foreach (var unityObj in Resources.FindObjectsOfTypeAll<UnityEngine.Object>())
            {
                if (unityObj is not null && unityObj.GetInstanceID() == instanceId)
                {
                    var actualType = typeResolver.GetActualType(unityObj);
                    return CastIfIl2Cpp(unityObj, actualType);
                }
            }
        }

        throw new KeyNotFoundException($"Registered object '{objectId}' was not found.");
    }

    private static object ReadDirectMemberValue(object instance, Type instanceType, string memberName)
    {
        var field = instanceType.GetField(memberName, InstanceMembers | BindingFlags.IgnoreCase);
        if (field is not null)
        {
            return field.GetValue(instance)
                   ?? throw new InvalidOperationException(
                       $"Field '{field.Name}' on '{FormatType(instanceType)}' is null.");
        }

        var property = instanceType.GetProperty(memberName, InstanceMembers | BindingFlags.IgnoreCase);
        if (property is not null && property.CanRead && property.GetIndexParameters().Length == 0)
        {
            return property.GetValue(instance)
                   ?? throw new InvalidOperationException(
                       $"Property '{property.Name}' on '{FormatType(instanceType)}' returned null.");
        }

        throw new MissingMemberException(instanceType.FullName, memberName);
    }

    private static int? ReadOptionalCount(object collection, Type collectionType)
    {
        var countProp = collectionType.GetProperty("Count", InstanceMembers)
                        ?? collectionType.GetProperty("Length", InstanceMembers);
        if (countProp is not null && countProp.CanRead && countProp.GetIndexParameters().Length == 0)
        {
            var raw = countProp.GetValue(collection);
            if (raw is int intCount)
            {
                return intCount;
            }
        }

        var sizeField = collectionType.GetField("_size", InstanceMembers);
        if (sizeField is not null && sizeField.GetValue(collection) is int fieldCount)
        {
            return fieldCount;
        }

        return null;
    }

    private static bool HasDictionaryShape(Type collectionType)
    {
        var genericArgs = collectionType.GetGenericArguments();
        if (genericArgs.Length == 2 &&
            (collectionType.GetMethod("ContainsKey", InstanceMembers) is not null ||
             collectionType.GetMethod("TryGetValue", InstanceMembers) is not null))
        {
            return true;
        }

        return collectionType.GetProperty("Keys", InstanceMembers) is not null &&
               collectionType.GetProperty("Values", InstanceMembers) is not null;
    }

    private bool TryExpandIl2CppDictionary(
        object collection,
        Type collectionType,
        int limit,
        List<CollectionEntry> entries,
        out int totalCount,
        out bool truncated)
    {
        totalCount = 0;
        truncated = false;

        if (!HasDictionaryShape(collectionType))
        {
            return false;
        }

        totalCount = ReadOptionalCount(collection, collectionType) ?? 0;
        var genericArgs = collectionType.GetGenericArguments();
        var valueHint = genericArgs.Length >= 2 ? genericArgs[1] : typeof(object);

        var getEnumerator = collectionType.GetMethod(
            "GetEnumerator",
            InstanceMembers,
            null,
            Type.EmptyTypes,
            null);
        if (getEnumerator is not null)
        {
            var enumerator = getEnumerator.Invoke(collection, null);
            if (enumerator is not null)
            {
                try
                {
                    var enumType = enumerator.GetType();
                    var moveNext = enumType.GetMethod("MoveNext", InstanceMembers, null, Type.EmptyTypes, null);
                    var currentProp = enumType.GetProperty("Current", InstanceMembers);
                    if (moveNext is not null && currentProp is not null)
                    {
                        while (entries.Count < limit &&
                               moveNext.Invoke(enumerator, null) is bool moved &&
                               moved)
                        {
                            var current = currentProp.GetValue(enumerator);
                            if (current is null)
                            {
                                continue;
                            }

                            var curType = GetActualRuntimeType(current);
                            var entryKey = ReadKeyValueMember(current, curType, "Key", "key");
                            var entryValue = ReadKeyValueMember(current, curType, "Value", "value");
                            entries.Add(CreateCollectionEntry(
                                ValueConverter.SafeToString(entryKey),
                                entryValue,
                                valueHint));
                        }

                        truncated = totalCount > entries.Count;
                        return true;
                    }
                }
                finally
                {
                    if (enumerator is IDisposable disposable)
                    {
                        disposable.Dispose();
                    }
                }
            }
        }

        // Fallback via Keys + indexer
        var keysProp = collectionType.GetProperty("Keys", InstanceMembers);
        var indexer = FindSingleParameterIndexer(collectionType, keyParamType: genericArgs.Length >= 1 ? genericArgs[0] : null);
        if (keysProp is not null && indexer is not null)
        {
            var keysObj = keysProp.GetValue(collection);
            if (keysObj is not null)
            {
                var keysType = GetActualRuntimeType(keysObj);
                var keysEnumMethod = keysType.GetMethod("GetEnumerator", InstanceMembers, null, Type.EmptyTypes, null);
                var keysEnumerator = keysEnumMethod?.Invoke(keysObj, null);
                if (keysEnumerator is not null)
                {
                    try
                    {
                        var enumType = keysEnumerator.GetType();
                        var moveNext = enumType.GetMethod("MoveNext", InstanceMembers, null, Type.EmptyTypes, null);
                        var currentProp = enumType.GetProperty("Current", InstanceMembers);
                        if (moveNext is not null && currentProp is not null)
                        {
                            while (entries.Count < limit &&
                                   moveNext.Invoke(keysEnumerator, null) is bool moved &&
                                   moved)
                            {
                                var k = currentProp.GetValue(keysEnumerator);
                                var v = indexer.GetValue(collection, new[] { k });
                                entries.Add(CreateCollectionEntry(
                                    ValueConverter.SafeToString(k),
                                    v,
                                    valueHint));
                            }

                            truncated = totalCount > entries.Count;
                            return true;
                        }
                    }
                    finally
                    {
                        if (keysEnumerator is IDisposable disposable)
                        {
                            disposable.Dispose();
                        }
                    }
                }
            }
        }

        return false;
    }

    private bool TryExpandIl2CppIndexedCollection(
        object collection,
        Type collectionType,
        int limit,
        List<CollectionEntry> entries,
        out int totalCount,
        out bool truncated)
    {
        totalCount = 0;
        truncated = false;

        var indexer = FindSingleParameterIndexer(collectionType, typeof(int));
        var count = ReadOptionalCount(collection, collectionType);
        if (indexer is null || !count.HasValue)
        {
            return false;
        }

        totalCount = count.Value;
        var genericArgs = collectionType.GetGenericArguments();
        var elemHint = genericArgs.Length >= 1 ? genericArgs[0] : indexer.PropertyType;
        var take = Math.Min(totalCount, limit);
        for (var i = 0; i < take; i++)
        {
            var element = indexer.GetValue(collection, new object[] { i });
            entries.Add(CreateCollectionEntry(
                i.ToString(CultureInfo.InvariantCulture),
                element,
                elemHint));
        }

        truncated = totalCount > limit;
        return true;
    }

    private bool TrySetIl2CppDictionaryEntry(
        object collection,
        Type collectionType,
        string key,
        string value,
        string normalizedPath,
        out StatusResponse response)
    {
        response = null!;
        if (!HasDictionaryShape(collectionType))
        {
            return false;
        }

        var genericArgs = collectionType.GetGenericArguments();
        var keyType = genericArgs.Length >= 1 ? genericArgs[0] : typeof(string);
        var valueType = genericArgs.Length >= 2 ? genericArgs[1] : typeof(object);
        var indexer = FindSingleParameterIndexer(collectionType, keyType)
                      ?? FindSingleParameterIndexer(collectionType, null);
        if (indexer is null || !indexer.CanWrite)
        {
            return false;
        }

        var effectiveKeyType = indexer.GetIndexParameters()[0].ParameterType;
        var effectiveValueType = valueType != typeof(object) ? valueType : indexer.PropertyType;
        EnsureEditableCollectionElementType(effectiveValueType);

        var convertedKey = ValueConverter.ConvertString(key, effectiveKeyType);
        var convertedValue = ValueConverter.ConvertString(value, effectiveValueType);
        indexer.SetValue(collection, convertedValue, new[] { convertedKey });
        response = new StatusResponse(
            "ok",
            $"Set dictionary entry '{key}' on '{normalizedPath}' ({FormatType(effectiveValueType)}).");
        return true;
    }

    private bool TrySetIl2CppIndexedEntry(
        object collection,
        Type collectionType,
        string key,
        string value,
        string normalizedPath,
        out StatusResponse response)
    {
        response = null!;
        var indexer = FindSingleParameterIndexer(collectionType, typeof(int));
        var count = ReadOptionalCount(collection, collectionType);
        if (indexer is null || !indexer.CanWrite || !count.HasValue)
        {
            return false;
        }

        var index = ParseCollectionIndex(key, count.Value);
        var genericArgs = collectionType.GetGenericArguments();
        var elementType = genericArgs.Length >= 1 ? genericArgs[0] : indexer.PropertyType;
        EnsureEditableCollectionElementType(elementType);
        var convertedValue = ValueConverter.ConvertString(value, elementType);
        indexer.SetValue(collection, convertedValue, new object[] { index });
        response = new StatusResponse(
            "ok",
            $"Set collection index [{index}] on '{normalizedPath}' ({FormatType(elementType)}).");
        return true;
    }

    private static PropertyInfo? FindSingleParameterIndexer(Type collectionType, Type? keyParamType)
    {
        PropertyInfo? fallback = null;
        foreach (var prop in collectionType.GetProperties(InstanceMembers))
        {
            var indexParams = prop.GetIndexParameters();
            if (indexParams.Length != 1)
            {
                continue;
            }

            if (keyParamType is null || indexParams[0].ParameterType == keyParamType)
            {
                return prop;
            }

            fallback ??= prop;
        }

        return keyParamType is null ? fallback : null;
    }

    private static object? ReadKeyValueMember(
        object current,
        Type currentType,
        string pascalName,
        string camelName)
    {
        var prop = currentType.GetProperty(pascalName, InstanceMembers | BindingFlags.IgnoreCase);
        if (prop is not null && prop.CanRead && prop.GetIndexParameters().Length == 0)
        {
            return prop.GetValue(current);
        }

        var field = currentType.GetField(pascalName, InstanceMembers | BindingFlags.IgnoreCase)
                    ?? currentType.GetField(camelName, InstanceMembers | BindingFlags.IgnoreCase);
        return field?.GetValue(current);
    }

    private static int ParseCollectionIndex(string key, int count)
    {
        if (!int.TryParse(key.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var index))
        {
            throw new ArgumentException($"Collection key '{key}' must be an integer index.");
        }

        if (index < 0 || index >= count)
        {
            throw new ArgumentOutOfRangeException(
                nameof(key),
                $"Collection index {index} is out of range [0, {count}).");
        }

        return index;
    }

    private static object ConvertDictionaryKey(IDictionary dict, Type keyType, string rawKey)
    {
        if (keyType != typeof(object))
        {
            return ValueConverter.ConvertString(rawKey, keyType)!;
        }

        foreach (var existingKey in dict.Keys)
        {
            if (existingKey is not null &&
                string.Equals(
                    ValueConverter.SafeToString(existingKey),
                    rawKey,
                    StringComparison.OrdinalIgnoreCase))
            {
                return existingKey;
            }
        }

        return rawKey;
    }

    private static void EnsureEditableCollectionElementType(Type elementType)
    {
        var unwrapped = Nullable.GetUnderlyingType(elementType) ?? elementType;
        if (unwrapped.IsPrimitive ||
            unwrapped.IsEnum ||
            unwrapped == typeof(string) ||
            unwrapped == typeof(decimal) ||
            unwrapped == typeof(Guid) ||
            unwrapped == typeof(Vector2) ||
            unwrapped == typeof(Vector3) ||
            unwrapped == typeof(Vector4) ||
            unwrapped == typeof(Quaternion) ||
            unwrapped == typeof(Color) ||
            unwrapped == typeof(object))
        {
            return;
        }

        throw new InvalidOperationException(
            $"Collection element type '{FormatType(elementType)}' is a non-primitive reference/struct type and cannot be set directly.");
    }

    private static string FormatType(Type type) => type.FullName ?? type.Name;

    private static string FormatMethod(MethodInfo method)
    {
        var parameters = string.Join(
            ", ",
            method.GetParameters().Select(parameter =>
                $"{FormatType(parameter.ParameterType)} {parameter.Name}"));
        return $"{FormatType(method.ReturnType)} {method.Name}({parameters})";
    }

    internal sealed record ResolvedComponent(
        GameObject GameObject,
        Component RawComponent,
        object Instance,
        Type Type);

    private sealed record SelectedMethod(MethodInfo Method, object?[] Arguments);
}
