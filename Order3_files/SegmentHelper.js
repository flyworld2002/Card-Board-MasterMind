const segmentHelper = {

    /**
     * send generic events
     * @param {any} eventName
     * @param {any} eventData
     */
    trackEvent: function (eventName, eventData) {
        // Change names followed by Segment's standards in past tense.
        switch (eventName) {
            case 'Marketplace Page View':
                eventName = 'Marketplace Page Viewed';
                break;
            case 'Order Complete View':
                eventName = 'Order Received';
                break;
            case 'Storefronts Pay Later Order Submitted':
                eventName = 'Storefronts Pay Later Order Submitted';
                break;
            case 'Cart View':
                eventName = 'Cart Viewed';
                break;
            case 'Cart Optimizer View':
                eventName = 'Cart Optimizer Viewed';
                break;
            case 'Cart Optimizer Complete':
                eventName = 'Cart Optimizer Completed';
                break;
            case 'Cart Optimizer Select Cart':
                eventName = 'Cart Optimizer Cart Selected';
                break;
            case 'Item Added to Cart View':
                eventName = 'Item Added to Cart Viewed';
                break;
            case 'Product Details View':
                eventName = 'Product Details Viewed';
                break;
            case 'Product Details Add to Cart':
                eventName = 'Product Details Added to Cart';
                break;
            case 'Shipping Checkout View':
                eventName = 'Shipping Checkout Viewed';
                break;
            case 'Review and Pay Checkout View':
                eventName = 'Review and Pay Checkout Viewed';
                break;
            case 'Marketplace Email Sign Up':
                eventName = 'Marketplace Email Signed Up';
                break;
            case 'LISTO Product Details View':
                eventName = 'LISTO Product Details Viewed';
                break;
            case 'LISTO Product Details Add to Cart':
                eventName = 'LISTO Product Details Added to Cart';
                break;
            default:
                console.warn('Don\'t recognize event name, leaving unchanged.');
        }

        // Change event property names to snake case.
        this.renameObjectProperties(eventData, this.convertCamelCaseToSnakeCase);

        // send
        analytics.track(eventName, eventData);
    },

    /**
     * Reset / Logout user
     */
    reset: function () {
        analytics.reset();
    },

    /**
     * Rename object properties with specified conversionMethod
     * @param {any} eventObject
     * @param {any} conversionMethod
     */
    renameObjectProperties: function (eventObject, conversionMethod) {
        const objectKeys = Object.keys(eventObject);
        objectKeys.forEach(function (propertyName) {
            const newPropertyName = conversionMethod(propertyName);
            if (newPropertyName !== propertyName) {
                eventObject[newPropertyName] = eventObject[propertyName]; // create new property and assign old value
                delete eventObject[propertyName]; // delete old property
            }
        });
    },

    /**
     * Camel case to snake case
     * @param {any} text
     */
    convertCamelCaseToSnakeCase: function (text) {
        return text.replace(/[A-Z]/g, letter => `_${letter.toLowerCase()}`);
    },

}
